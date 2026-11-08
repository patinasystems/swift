//===--- InstructionChangeTracker.h - bounded SIL dirty regions -*- C++ -*-===//
//
// This source file is part of the Swift.org open source project
//
// Copyright (c) 2026 Apple Inc. and the Swift project authors
// Licensed under Apache License v2.0 with Runtime Library Exception
//
// See https://swift.org/LICENSE.txt for license information
// See https://swift.org/CONTRIBUTORS.txt for the list of Swift project authors
//
//===----------------------------------------------------------------------===//

#ifndef SWIFT_SILOPTIMIZER_UTILS_INSTRUCTIONCHANGETRACKER_H
#define SWIFT_SILOPTIMIZER_UTILS_INSTRUCTIONCHANGETRACKER_H

#include "swift/SIL/ApplySite.h"
#include "swift/SIL/SILFunction.h"
#include "swift/SIL/SILInstruction.h"
#include "llvm/ADT/SetVector.h"
#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/ADT/SmallVector.h"
#include <cstdint>

namespace swift {

/// A bounded, pass-local journal of instruction mutation neighborhoods.
///
/// Clients may reuse a dirty region only when ALL mutations in an epoch were
/// reported. This is not a general SIL mutation observer: an unaudited helper,
/// CFG/callee/effects change, ownership change, or other unknown mutation must
/// call requireFullScan before any saved region is consumed. A no-change helper
/// need not invalidate an otherwise complete journal.
///
/// Only block pointers survive a notification. CFG changes must invalidate the
/// journal before blocks are removed. Insertion/deletion sets contain identity
/// tokens only, never dereferenced; delayed instruction deletion keeps those
/// identities unique until the end of the pass. Do not retain the journal across
/// SILModule::flushDeletedInsts or across function passes.
class InstructionChangeTracker {
  static constexpr unsigned MaxDirtyBlocks = 64;
  static constexpr unsigned MaxNeighborhoodVisits = 256;
  static constexpr unsigned MaxInstructionEvents = 256;

  SILFunction &function;
  llvm::SmallSetVector<SILBasicBlock *, 16> dirtyBlocks;
  llvm::SmallPtrSet<SILInstruction *, 32> insertedInstructions;
  llvm::SmallPtrSet<SILInstruction *, 32> deletedInstructions;
  unsigned neighborhoodVisits = 0;
  uint64_t mutationCount = 0;
  int64_t instructionCountChange = 0;
  const char *fallbackReason = "initial scan";

  bool recordBlock(SILInstruction *inst) {
    if (!inst || inst->isDeleted() || !inst->getParent() ||
        inst->getParent()->getParent() != &function) {
      requireFullScan("unavailable instruction");
      return false;
    }
    dirtyBlocks.insert(inst->getParent());
    if (dirtyBlocks.size() > MaxDirtyBlocks) {
      requireFullScan("dirty block limit");
      return false;
    }
    return true;
  }

  bool isLocalValue(SILValue value) {
    return value && !value->getType().isAddress() &&
           value->getOwnershipKind() == OwnershipKind::None &&
           value->getType().isTrivial(function);
  }

  bool supportsMutation(SILInstruction *inst) {
    if (!recordBlock(inst))
      return false;
    if (isa<TermInst>(inst) || FullApplySite::isa(inst) ||
        inst->mayHaveSideEffects()) {
      requireFullScan("nonlocal instruction mutation");
      return false;
    }
    if (inst->getNumOperands() > MaxNeighborhoodVisits ||
        inst->getResults().size() > MaxNeighborhoodVisits) {
      requireFullScan("wide instruction mutation");
      return false;
    }
    for (auto &operand : inst->getAllOperands()) {
      if (!isLocalValue(operand.get())) {
        requireFullScan("address ownership or dropped operand");
        return false;
      }
    }
    for (auto result : inst->getResults()) {
      if (!isLocalValue(result)) {
        requireFullScan("address or ownership result");
        return false;
      }
    }
    return true;
  }

public:
  explicit InstructionChangeTracker(SILFunction &function) : function(function) {}

  /// Start a fresh, explicitly covered mutation epoch. The serial is monotonic
  /// across epochs, so a client can distinguish observation from a mutation.
  void reset() {
    dirtyBlocks.clear();
    insertedInstructions.clear();
    deletedInstructions.clear();
    neighborhoodVisits = 0;
    instructionCountChange = 0;
    fallbackReason = nullptr;
  }

  /// The first reason wins. Reasons must have static lifetime.
  void requireFullScan(const char *reason = "unknown mutation") {
    if (!fallbackReason)
      fallbackReason = reason;
    dirtyBlocks.clear();
    insertedInstructions.clear();
    deletedInstructions.clear();
    instructionCountChange = 0;
  }

  bool requiresFullScan() const { return fallbackReason != nullptr; }
  const char *getFallbackReason() const { return fallbackReason; }
  ArrayRef<SILBasicBlock *> getDirtyBlocks() const {
    return dirtyBlocks.getArrayRef();
  }
  bool containsDirtyBlock(SILBasicBlock *block) const {
    return dirtyBlocks.contains(block);
  }
  uint64_t getMutationCount() const { return mutationCount; }
  /// Valid only while requiresFullScan() is false and all instruction creation
  /// and deletion went through the journal.
  int64_t getInstructionCountChange() const { return instructionCountChange; }

  /// Capture the old neighborhood BEFORE a mutation. Unchanged neighboring
  /// calls and terminators merely dirty their blocks; observing them is safe.
  /// Include downstream users transitively, and users of the old producers, so
  /// losing a use also revisits siblings whose hasOneUse predicates changed.
  void observeInstruction(SILInstruction *inst) {
    if (requiresFullScan())
      return;
    llvm::SmallVector<SILInstruction *, 16> worklist;
    llvm::SmallPtrSet<SILInstruction *, 32> visited;
    auto enqueue = [&](SILInstruction *candidate) {
      if (requiresFullScan() || !candidate)
        return;
      if (++neighborhoodVisits > MaxNeighborhoodVisits) {
        requireFullScan("neighborhood visit limit");
        return;
      }
      if (visited.insert(candidate).second)
        worklist.push_back(candidate);
    };
    if (!recordBlock(inst))
      return;
    if (inst->getNumOperands() > MaxNeighborhoodVisits) {
      requireFullScan("wide instruction neighborhood");
      return;
    }
    enqueue(inst);
    for (auto &operand : inst->getAllOperands()) {
      SILValue value = operand.get();
      if (!value) {
        requireFullScan("dropped operand neighborhood");
        return;
      }
      if (auto *producer = value->getDefiningInstruction())
        enqueue(producer);
      // Arguments do not have a producer instruction. Their other users can
      // still change behavior when this use disappears (e.g. hasOneUse).
      for (auto *use : value->getUses()) {
        enqueue(use->getUser());
        if (requiresFullScan())
          return;
      }
    }
    while (!worklist.empty() && !requiresFullScan()) {
      auto *current = worklist.pop_back_val();
      if (!recordBlock(current))
        return;
      for (auto result : current->getResults()) {
        for (auto *use : result->getUses()) {
          enqueue(use->getUser());
          if (requiresFullScan())
            return;
        }
      }
    }
  }

  void recordInstruction(SILInstruction *inst) {
    ++mutationCount;
    if (!requiresFullScan() && supportsMutation(inst))
      observeInstruction(inst);
  }

  void recordUnknownMutation(const char *reason = "unknown mutation") {
    ++mutationCount;
    requireFullScan(reason);
  }

  void recordInsertion(SILInstruction *inst) {
    if (requiresFullScan() || insertedInstructions.contains(inst))
      return;
    ++mutationCount;
    if (insertedInstructions.size() + deletedInstructions.size() >=
        MaxInstructionEvents) {
      requireFullScan("instruction event limit");
      return;
    }
    if (!supportsMutation(inst))
      return;
    insertedInstructions.insert(inst);
    ++instructionCountChange;
    observeInstruction(inst);
  }

  /// Call before dropping references. A duplicate actual-delete notification
  /// is harmless, but an initial notification after reference dropping forces
  /// the full scan rather than trusting a partial neighborhood.
  void recordDeletion(SILInstruction *inst) {
    if (requiresFullScan() || deletedInstructions.contains(inst))
      return;
    ++mutationCount;
    if (insertedInstructions.size() + deletedInstructions.size() >=
        MaxInstructionEvents) {
      requireFullScan("instruction event limit");
      return;
    }
    if (!supportsMutation(inst))
      return;
    observeInstruction(inst);
    if (!requiresFullScan()) {
      deletedInstructions.insert(inst);
      --instructionCountChange;
    }
  }

  /// Must precede use->set, including every operand updated by RAUW.
  void recordOperandChange(Operand *use, SILValue newValue) {
    if (requiresFullScan() || use->get() == newValue)
      return;
    ++mutationCount;
    if (!isLocalValue(newValue) || !supportsMutation(use->getUser())) {
      requireFullScan("nonlocal operand mutation");
      return;
    }
    observeInstruction(use->getUser());
    if (!requiresFullScan()) {
      if (auto *producer = newValue->getDefiningInstruction()) {
        observeInstruction(producer);
      } else {
        for (auto *newUse : newValue->getUses()) {
          observeInstruction(newUse->getUser());
          if (requiresFullScan())
            return;
        }
      }
    }
  }
};

} // namespace swift

#endif
