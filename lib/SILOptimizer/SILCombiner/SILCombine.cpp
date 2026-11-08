//===--- SILCombine.cpp ---------------------------------------------------===//
//
// This source file is part of the Swift.org open source project
//
// Copyright (c) 2014 - 2017 Apple Inc. and the Swift project authors
// Licensed under Apache License v2.0 with Runtime Library Exception
//
// See https://swift.org/LICENSE.txt for license information
// See https://swift.org/CONTRIBUTORS.txt for the list of Swift project authors
//
//===----------------------------------------------------------------------===//
//
// A port of LLVM's InstCombine pass to SIL. Its main purpose is for performing
// small combining operations/peepholes at the SIL level. It additionally
// performs dead code elimination when it initially adds instructions to the
// work queue in order to reduce compile time by not visiting trivially dead
// instructions.
//
//===----------------------------------------------------------------------===//

#define DEBUG_TYPE "sil-combine"

#include "SILCombiner.h"
#include "swift/Basic/Assertions.h"
#include "swift/SIL/BasicBlockDatastructures.h"
#include "swift/SIL/DebugUtils.h"
#include "swift/SIL/SILBuilder.h"
#include "swift/SIL/Test.h"
#include "swift/SILOptimizer/Analysis/AliasAnalysis.h"
#include "swift/SILOptimizer/Analysis/DominanceAnalysis.h"
#include "swift/SILOptimizer/Analysis/NonLocalAccessBlockAnalysis.h"
#include "swift/SILOptimizer/Analysis/SimplifyInstruction.h"
#include "swift/SILOptimizer/PassManager/PassManager.h"
#include "swift/SILOptimizer/PassManager/Transforms.h"
#include "swift/SILOptimizer/Utils/BasicBlockOptUtils.h"
#include "swift/SILOptimizer/Utils/CanonicalizeInstruction.h"
#include "swift/SILOptimizer/Utils/CFGOptUtils.h"
#include "swift/SILOptimizer/Utils/DebugOptUtils.h"
#include "swift/SILOptimizer/Utils/InstOptUtils.h"
#include "swift/SILOptimizer/Utils/OSSACanonicalizeGuaranteed.h"
#include "swift/SILOptimizer/Utils/OSSACanonicalizeOwned.h"
#include "swift/SILOptimizer/Utils/SILOptFunctionBuilder.h"
#include "swift/SILOptimizer/Utils/StackNesting.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/Statistic.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/Debug.h"
#include "llvm/Support/ErrorHandling.h"
#include <cstdlib>

using namespace swift;

STATISTIC(NumCombined, "Number of instructions combined");
STATISTIC(NumDeadInst, "Number of dead insts eliminated");

static llvm::cl::opt<bool> EnableSinkingOwnedForwardingInstToUses(
    "silcombine-owned-code-sinking",
    llvm::cl::desc("Enable sinking of owned forwarding insts"),
    llvm::cl::init(true), llvm::cl::Hidden);

// Allow disabling general optimization for targeted unit tests.
static llvm::cl::opt<bool> EnableSILCombineCanonicalize(
    "sil-combine-canonicalize",
    llvm::cl::desc("Canonicalization during sil-combine"), llvm::cl::init(true),
    llvm::cl::Hidden);

static llvm::cl::opt<bool> EnableChangedRegions(
    "sil-combine-changed-regions", llvm::cl::Hidden,
    llvm::cl::init([] {
      auto *value = std::getenv("SWIFT_ENABLE_CHANGED_REGION_REOPT");
      return value && StringRef(value) == "1";
    }()));
static llvm::cl::opt<bool> VerifyChangedRegions(
    "sil-combine-verify-changed-regions", llvm::cl::Hidden,
    llvm::cl::init(false));
static llvm::cl::opt<bool> PrintChangedRegions(
    "sil-combine-print-changed-regions", llvm::cl::Hidden,
    llvm::cl::init(false));

//===----------------------------------------------------------------------===//
//                              Utility Methods
//===----------------------------------------------------------------------===//

/// addReachableCodeToWorklist - Walk the function in depth-first order, adding
/// all reachable code to the worklist.
///
/// This has a couple of tricks to make the code faster and more powerful.  In
/// particular, we DCE instructions as we go, to avoid adding them to the
/// worklist (this significantly speeds up SILCombine on code where many
/// instructions are dead or constant).
void SILCombiner::addReachableCodeToWorklist(
    SILBasicBlock *BB,
    const llvm::SmallPtrSetImpl<SILBasicBlock *> *dirtyBlocks) {
  BasicBlockWorklist Worklist(BB);
  llvm::SmallVector<SILInstruction *, 16> InstrsForSILCombineWorklist;

  while (SILBasicBlock *BB = Worklist.pop()) {
    // Still traverse clean blocks: selected blocks must retain the original
    // reachable DFS/top-down ordering, independent of notification order.
    if (!dirtyBlocks || dirtyBlocks->contains(BB)) {
      for (SILBasicBlock::iterator BBI = BB->begin(), E = BB->end(); BBI != E; ) {
        SILInstruction *Inst = &*BBI;
        ++BBI;

        // DCE instruction if trivially dead.
        if (isInstructionTriviallyDead(Inst)) {
          ++NumDeadInst;
          LLVM_DEBUG(llvm::dbgs() << "SC: DCE: " << *Inst << '\n');

          // We pass in false here since the worklist is not initialized yet.
          eraseInstFromFunction(*Inst, BBI,
                                false /*Don't add operands to worklist*/);
          continue;
        }

        InstrsForSILCombineWorklist.push_back(Inst);
      }
    }

    // Recursively visit successors.
    for (SILBasicBlock *Succ : BB->getSuccessors()) {
      Worklist.pushIfNotVisited(Succ);
    }
  }

  // Once we've found all of the instructions to add to the worklist, add them
  // in reverse order. This way SILCombine will visit from the top of the
  // function down. This jives well with the way that it adds all uses of
  // instructions to the worklist after doing a transformation, thus avoiding
  // some N^2 behavior in pathological cases.
  addInitialGroup(InstrsForSILCombineWorklist);
}

//===----------------------------------------------------------------------===//
//                               Implementation
//===----------------------------------------------------------------------===//

namespace swift {

// Define a CanonicalizeInstruction subclass for use in SILCombine.
class SILCombineCanonicalize final : CanonicalizeInstruction {
  SmallSILInstructionWorklist<256> &Worklist;
  InstructionChangeTracker *changeTracker;
  bool changed = false;

public:
  SILCombineCanonicalize(SmallSILInstructionWorklist<256> &Worklist,
                         DeadEndBlocks &deadEndBlocks,
                         InstructionChangeTracker *tracker = nullptr)
      : CanonicalizeInstruction(DEBUG_TYPE, deadEndBlocks), Worklist(Worklist),
        changeTracker(tracker) {
    // Keep the precise pre-set callback separate from notifyHasNewUsers,
    // which also has callers that perform raw RAUW without an old-use event.
    callbacks = callbacks.onSetUseValue([&](Operand *use, SILValue newValue) {
      use->set(newValue);
      if (Worklist.size() < 10000)
        Worklist.addUsersToWorklist(newValue);
      changed = true;
    }).withChangeTracker(tracker);
  }

  void notifyNewInstruction(SILInstruction *inst) override {
    if (changeTracker)
      changeTracker->recordInsertion(inst);
    Worklist.add(inst);
    Worklist.addUsersOfAllResultsToWorklist(inst);
    changed = true;
  }

  // Just delete the given 'inst' and record its operands. The callback isn't
  // allowed to mutate any other instructions.
  void killInstruction(SILInstruction *inst) override {
    if (changeTracker)
      changeTracker->recordDeletion(inst);
    Worklist.eraseSingleInstFromFunction(*inst,
                                         /*AddOperandsToWorklist*/ true);
    changed = true;
  }

  void notifyHasNewUsers(SILValue value) override {
    if (changeTracker)
      changeTracker->requireFullScan("untracked-canonicalizer-rauw");
    if (Worklist.size() < 10000) {
      Worklist.addUsersToWorklist(value);
    }
    changed = true;
  }

  bool tryCanonicalize(SILInstruction *inst) {
    if (!EnableSILCombineCanonicalize)
      return false;

    changed = false;
    canonicalize(inst);
    return changed;
  }
};

} // end namespace swift

SILCombiner::SILCombiner(SILFunctionTransform *trans,
                         bool removeCondFails, bool enableCopyPropagation) :
  parentTransform(trans),
  AA(trans->getPassManager()->getAnalysis<AliasAnalysis>(trans->getFunction())),
  CA(trans->getPassManager()->getAnalysis<BasicCalleeAnalysis>()),
  DA(trans->getPassManager()->getAnalysis<DominanceAnalysis>()),
  PCA(trans->getPassManager()->getAnalysis<ProtocolConformanceAnalysis>()),
  CHA(trans->getPassManager()->getAnalysis<ClassHierarchyAnalysis>()),
  NLABA(trans->getPassManager()->getAnalysis<NonLocalAccessBlockAnalysis>()),
  Worklist("SC"),
  deleter(InstModCallbacks()
              .onDelete([&](SILInstruction *instToDelete) {
                // We allow for users in SILCombine to perform 2 stage
                // deletion, so we need to split the erasing of
                // instructions from adding operands to the worklist.
                eraseInstFromFunction(*instToDelete,
                                      false /* don't add operands */);
              })
              .onNotifyWillBeDeleted(
                  [&](SILInstruction *instThatWillBeDeleted) {
                    Worklist.addOperandsToWorklist(
                      *instThatWillBeDeleted);
                  })
              .onCreateNewInst([&](SILInstruction *newlyCreatedInst) {
                Worklist.add(newlyCreatedInst);
              })
              .onSetUseValue([&](Operand *use, SILValue newValue) {
                use->set(newValue);
                Worklist.add(use->getUser());
              })),
  DEBA(trans->getPassManager()->getAnalysis<DeadEndBlocksAnalysis>()), 
  MadeChange(false), RemoveCondFails(removeCondFails),
  enableCopyPropagation(enableCopyPropagation), Iteration(0),
  Builder(*trans->getFunction(), &TrackingList),
  FuncBuilder(*trans),
  CastOpt(
      FuncBuilder, nullptr /*SILBuilderContext*/,
      /* ReplaceValueUsesAction */
      [&](SILValue Original, SILValue Replacement) {
        replaceValueUsesWith(Original, Replacement);
      },
      /* ReplaceInstUsesAction */
      [&](SingleValueInstruction *I, ValueBase *V) {
        replaceInstUsesWith(*I, V);
      },
      /* EraseAction */
      [&](SILInstruction *I) { eraseInstFromFunction(*I); }),
  deBlocks(trans->getFunction()),
  ownershipFixupContext(getInstModCallbacks(), deBlocks) {
  auto *invocation =
      trans->getPassManager()->getSwiftPassInvocation()->getCurrent();
  invocation->injectSILCombiner(this);
  if (EnableChangedRegions) {
    changeTracker.emplace(*trans->getFunction());
    deleter.getCallbacks() =
        deleter.getCallbacks().withChangeTracker(getChangeTracker());
    invocation->setInstructionChangeTracker(getChangeTracker());
  }
}

SILCombiner::~SILCombiner() {
  if (changeTracker)
    parentTransform->getPassManager()->getSwiftPassInvocation()->getCurrent()
        ->setInstructionChangeTracker(nullptr);
}

bool SILCombiner::trySinkOwnedForwardingInst(SingleValueInstruction *svi) {
  if (auto *consumingUse = svi->getSingleConsumingUse()) {
    auto *consumingUser = consumingUse->getUser();

    // If our user is already in the same block, we don't move it further.
    if (svi->getParent() == consumingUser->getParent())
      return false;

    // Otherwise, make sure our instruction does not have any non-debug uses
    // that are non-lifetime ending. If so, we return.
    if (llvm::any_of(getNonDebugUses(svi),
                     [](Operand *use) { return !use->isLifetimeEnding(); }))
      return false;

    LLVM_DEBUG(llvm::dbgs() << "Sink forwarding: " << *svi << '\n');

    // Otherwise, salvage all of the debug uses so we don't have to sink them
    // as well and then return true so we process svi in its new position.
    deleteAllDebugUses(svi, getInstModCallbacks());
    svi->moveBefore(consumingUser);
    MadeChange = true;

    // NOTE: We return false here so that our caller doesn't delete the
    // instruction and instead tries to simplify it.
    return false;
  }

  // If we have multiple consuming uses, then we know that our
  // forwarding inst must be live out of the current block and thus we
  // might be able to duplicate/sink.
  if (llvm::any_of(getNonDebugUses(svi),
                   [](Operand *use) { return !use->isLifetimeEnding(); }))
    return false;

  // Salvage svi before sinking it.
  salvageDebugInfo(svi);

  while (!svi->use_empty()) {
    auto *sviUse = *svi->use_begin();
    auto *sviUser = sviUse->getUser();

    if (auto *dvi = dyn_cast<DestroyValueInst>(sviUser)) {
      dvi->setOperand(svi->getOperand(0));
      Worklist.add(dvi);
      continue;
    }

    if (sviUser->isDebugInstruction()) {
      eraseInstFromFunction(*sviUser);
      continue;
    }

    auto *newSVI = svi->clone(sviUser);

    LLVM_DEBUG(llvm::dbgs()
               << "Sink forwarding: " << *svi << " to " << *newSVI << '\n');

    Worklist.add(newSVI);
    sviUse->set(newSVI);
  }

  eraseInstFromFunction(*svi);
  MadeChange = true;
  return true;
}

/// Canonicalize each extended OSSA lifetime that contains an instruction newly
/// created during this SILCombine iteration.
///
/// \p currentInst is null if the current instruction was deleted during its
/// SILCombine.
///
/// Avoid endless worklist iteration as follows:
///
/// - Canonicalization only runs on the canonical definition of the visited
///   instruction if it was itself a copy or any new copies were inserted
///   as a result of optimization.
///
/// - Instructions are only added back to the SILCombine worklist when
///   canonicalization deletes an instruction. Only the canonical def being
///   processed and its uses are added rather than arbitrary operands of the
///   deleted instruction. This ensures that an instruction is only added back
///   to the worklist after SILCombine either directly optimized it or created a
///   new copy_value for which it is the canonical def or its use.
void SILCombiner::canonicalizeOSSALifetimes(SILInstruction *currentInst) {
  if (!enableCopyPropagation || !Builder.hasOwnership())
    return;

  // For very large functions OSSA canonicalization can run into noticeable quadratic
  // behavior. Don't canonicalize functions with more than 100000 SIL instructions.
  // This limit is large enough to not affect most of real-world SIL functions.
  if (initialWorklistSize > 100000)
    return;

  llvm::SmallSetVector<SILValue, 16> defsToCanonicalize;

  // copyInst was either optimized by a SILCombine visitor or is a copy_value
  // produced by the visitor. Find the canonical def.
  auto recordCopiedDef = [&defsToCanonicalize](CopyValueInst *copyInst) {
    SILValue def = OSSACanonicalizeOwned::getCanonicalCopiedDef(copyInst);

    // getCanonicalCopiedDef returns a copy whenever that the copy's source is
    // guaranteed. In that case, find the root of the borrowed lifetime. If it
    // is a function argument, then a simple guaranteed canonicalization can be
    // performed. Canonicalizing other borrow scopes is not handled by
    // SILCombine because it's not a single-lifetime canonicalization.  Instead,
    // SILCombine treats a copy that uses a borrowed value as a separate owned
    // live range. Handling the compensation code across the borrow scope
    // boundary requires post processing in a particular order.  The copy
    // propagation pass knows how to handle that. To avoid complexity and ensure
    // fast convergence, rewriting borrow scopes should not be combined with
    // other unrelated transformations.
    if (auto *copyDef = dyn_cast<CopyValueInst>(def)) {
      if (SILValue borrowDef =
              OSSACanonicalizeGuaranteed::getCanonicalBorrowedDef(
                  copyDef->getOperand())) {
        if (isa<SILFunctionArgument>(borrowDef)) {
          def = borrowDef;
        }
      }
    }
    defsToCanonicalize.insert(def);
  };

  if (auto *copyInst = dyn_cast_or_null<CopyValueInst>(currentInst))
    recordCopiedDef(copyInst);

  for (auto *trackedInst : *Builder.getTrackingList()) {
    if (trackedInst->isDeleted())
      continue;
    if (auto *copyInst = dyn_cast<CopyValueInst>(trackedInst))
      recordCopiedDef(copyInst);
  }
  if (defsToCanonicalize.empty())
    return;
  if (changeTracker)
    changeTracker->requireFullScan("ownership-lifetimes");

  // Remove instructions deleted during canonicalization from SILCombine's
  // worklist. OSSACanonicalizeOwned invalidates operands before invoking
  // the deletion callback.
  auto canonicalizeCallbacks =
      InstModCallbacks().onDelete([this](SILInstruction *instToDelete) {
        eraseInstFromFunction(*instToDelete,
                              false /*do not add operands to the worklist*/);
      });
  InstructionDeleter deleter(std::move(canonicalizeCallbacks));

  DominanceInfo *domTree = DA->get(&Builder.getFunction());
  OSSACanonicalizeOwned canonicalizer(
      DontPruneDebugInsts,
      MaximizeLifetime_t(!parentTransform->getFunction()->shouldOptimize()),
      parentTransform->getFunction(), NLABA, DEBA, domTree, CA, deleter);
  OSSACanonicalizeGuaranteed borrowCanonicalizer(parentTransform->getFunction(),
                                                 deleter);

  while (!defsToCanonicalize.empty()) {
    SILValue def = defsToCanonicalize.pop_back_val();

    deleter.getCallbacks().resetHadCallbackInvocation();

    auto canonicalized = [&]() {
      if (!deleter.getCallbacks().hadCallbackInvocation())
        return;

      if (auto *inst = def->getDefiningInstruction()) {
        Worklist.add(inst);
      }
      for (auto *use : def->getUses()) {
        Worklist.add(use->getUser());
      }
    };

    if (def->getOwnershipKind() == OwnershipKind::Guaranteed) {
      if (auto functionArg = dyn_cast<SILFunctionArgument>(def)) {
        if (borrowCanonicalizer.canonicalizeFunctionArgument(functionArg))
          canonicalized();
      }
      continue;
    }
    if (canonicalizer.canonicalizeValueLifetime(def)) {
      canonicalized();
    }
  }
}

bool SILCombiner::doOneIteration(SILFunction &F, unsigned Iteration,
                                 bool forceFullScan) {
  MadeChange = false;

  LLVM_DEBUG(llvm::dbgs() << "\n\nSILCOMBINE ITERATION #" << Iteration << " on "
                          << F.getName() << "\n");

  llvm::SmallPtrSet<SILBasicBlock *, 16> dirtyBlocks;
  // Keep regional worklists well below notifyHasNewUsers' 10000-entry
  // profitability cap, even with the journal's bounded insertion/deletion
  // events. Changing that cap's outcome could change optimization quality.
  if (changeTracker && fullWorklistSize >= 8192)
    changeTracker->requireFullScan("worklist-size-limit");
  bool importedSeed = !Iteration && !importedDirtyBlocks.empty();
  lastIterationWasRegional = changeTracker && !forceFullScan &&
      !changeTracker->requiresFullScan() &&
      (importedSeed || (Iteration && !changeTracker->getDirtyBlocks().empty()));
  const char *fallback = changeTracker && changeTracker->requiresFullScan()
                             ? changeTracker->getFallbackReason() : "none";
  if (lastIterationWasRegional) {
    if (importedSeed) {
      // The pass manager has already applied the producer's checked net
      // instruction delta. No instruction identity survives that handoff.
      dirtyBlocks.insert(importedDirtyBlocks.begin(), importedDirtyBlocks.end());
    } else {
      dirtyBlocks.insert(changeTracker->getDirtyBlocks().begin(),
                         changeTracker->getDirtyBlocks().end());
      if (!updateFullWorklistSize(changeTracker->getInstructionCountChange() -
                                 countChangeAfterSeed))
        lastIterationWasRegional = false;
    }
  }
  importedDirtyBlocks.clear();
  if (changeTracker)
    changeTracker->reset();

  addReachableCodeToWorklist(&*F.begin(),
                             lastIterationWasRegional ? &dirtyBlocks : nullptr);

  // If seeding itself encountered an untracked utility or lost count
  // coverage, rebuild the original complete seed before running transforms.
  // Never let an unknown count change the original profitability thresholds.
  if (lastIterationWasRegional &&
      (changeTracker->requiresFullScan() ||
       !updateFullWorklistSize(changeTracker->getInstructionCountChange()))) {
    while (!Worklist.isEmpty())
      Worklist.pop_back_val();
    Worklist.resetChecked();
    lastIterationWasRegional = false;
    addReachableCodeToWorklist(&*F.begin());
  }

  if (lastIterationWasRegional) {
    ++regionalSeeds;
    regionalSeedInstructions += Worklist.size();
    if (importedSeed) {
      ++crossPassSeeds;
      crossPassSeedInstructions += Worklist.size();
    }
  } else {
    ++fullSeeds;
    fullSeedInstructions += Worklist.size();
    fullWorklistSize = Worklist.size();
  }
  // Retain the original whole-function ownership complexity threshold even
  // when only a subset of instructions was seeded into this round.
  initialWorklistSize = fullWorklistSize;
  countChangeAfterSeed = changeTracker && !changeTracker->requiresFullScan()
                            ? changeTracker->getInstructionCountChange() : 0;
  if (PrintChangedRegions)
    llvm::errs() << "silcombine-region-round function=" << F.getName()
                 << " regional=" << lastIterationWasRegional
                 << " cross_pass=" << (lastIterationWasRegional && importedSeed)
                 << " instructions=" << Worklist.size()
                 << " fallback=" << fallback << '\n';

  SILCombineCanonicalize scCanonicalize(Worklist, *DEBA->get(&F),
                                        getChangeTracker());

  // Process until we run out of items in our worklist.
  while (!Worklist.isEmpty()) {
    SILInstruction *I = Worklist.pop_back_val();

    // When we erase an instruction, we use the map in the worklist to check if
    // the instruction is in the worklist. If it is, we replace it with null
    // instead of shifting all members of the worklist towards the front. This
    // check makes sure that if we run into any such residual null pointers, we
    // skip them.
    if (I == nullptr)
      continue;

    if (!parentTransform->continueWithNextSubpassRun(I)) {
      aborted = true;
      lastIterationWasRegional = false;
      return false;
    }

    // Visitor helpers report changes through this field as well as the
    // reference passed to processInstruction. Do not let an earlier mutation
    // hide whether this particular visit changed an unsupported instruction.
    bool earlierChange = MadeChange;
    MadeChange = false;
    processInstruction(I, scCanonicalize, MadeChange);
    MadeChange |= earlierChange;
  }

  Worklist.resetChecked();
  return MadeChange;
}

static bool hasAddressOperands(SILInstruction *inst) {
  for (Operand *op : inst->getRealOperands()) {
    if (op->get()->getType().isAddress())
      return true;
  }
  return false;
}

void SILCombiner::processInstruction(SILInstruction *I,
                                     SILCombineCanonicalize &scCanonicalize,
                                     bool &MadeChange) {
  // Check to see if we can DCE the instruction.
  if (isInstructionTriviallyDead(I)) {
    LLVM_DEBUG(llvm::dbgs() << "SC: DCE: " << *I << '\n');
    eraseInstFromFunction(*I);
    ++NumDeadInst;
    MadeChange = true;
    return;
  }
#ifndef NDEBUG
  std::string OrigIStr;
#endif
  LLVM_DEBUG(llvm::raw_string_ostream SS(OrigIStr); I->print(SS);
             OrigIStr = SS.str(););
  LLVM_DEBUG(llvm::dbgs() << "SC: Visiting: " << OrigIStr << '\n');

  // Canonicalize the instruction.
  if (scCanonicalize.tryCanonicalize(I)) {
    MadeChange = true;
    return;
  }

  // If we have reached this point, all attempts to do simple simplifications
  // have failed. First if we have an owned forwarding value, we try to
  // sink. Otherwise, we perform the actual SILCombine operation.
  if (EnableSinkingOwnedForwardingInstToUses) {
    // If we have an ownership forwarding single value inst that forwards
    // through its first argument and it is trivially duplicatable, see if it
    // only has consuming uses. If so, we can duplicate the instruction into
    // the consuming use blocks and destroy any destroy_value uses of it that
    // we see. This makes it easier for SILCombine to fold instructions with
    // owned parameters since chains of these values will be in the same
    // block.
    if (auto *svi = dyn_cast<SingleValueInstruction>(I)) {
      if (auto fwdOp = ForwardingOperation(svi)) {
        if (fwdOp.getSingleForwardingOperand() &&

            // Don't risk sinking instructions with address operands out of the
            // addressed memory's lifetime. E.g:
            // ```
            //   %3 = mark_dependence %2 on %1 : $*T  // must not be moved after the destroy_addr
            //   destroy_addr %1
            // ```
            !hasAddressOperands(svi) &&

            // Don't risk sinking mark_dependence out of the lifetime of the base value.
            !isa<MarkDependenceInst>(svi) &&

            SILValue(svi)->getOwnershipKind() == OwnershipKind::Owned) {
          // Try to sink the value. If we sank the value and deleted it,
          // return. If we didn't optimize or sank but we are still able to
          // optimize further, we fall through to SILCombine below.
          bool sunk = trySinkOwnedForwardingInst(svi);
          if (changeTracker && MadeChange)
            changeTracker->requireFullScan("ownership-sinking");
          if (sunk) {
            return;
          }
        }
      }
    }
  }

  // Then begin... SILCombine.
  Builder.setInsertionPoint(I);

  SILInstruction *currentInst = I;
  auto mutationCount = changeTracker ? changeTracker->getMutationCount() : 0;
  // The legacy tuple-extract visitor only inserts a literal replacement.
  // Other C++ visitors can mutate raw operands or global/ownership state:
  // their existing return/MadeChange contract, together with recorded events,
  // is a coverage gate, not an assumption that all setters are observed.
  bool auditedVisitor = isa<TupleExtractInst>(I);
  SILInstruction *Result = visit(I);
  if (changeTracker && !auditedVisitor &&
      (Result || MadeChange ||
       changeTracker->getMutationCount() != mutationCount))
    changeTracker->requireFullScan("unaudited-visitor");
  if (Result) {
    ++NumCombined;
    if (changeTracker) {
      if (Result == I) {
        changeTracker->requireFullScan("in-place-visitor");
      } else {
        for (auto pair : llvm::zip(I->getResults(), Result->getResults()))
          trackReplacement(std::get<0>(pair), std::get<1>(pair));
        changeTracker->recordInsertion(Result);
        changeTracker->recordDeletion(I);
      }
    }
    // Should we replace the old instruction with a new one?
    Worklist.replaceInstructionWithInstruction(I, Result
#ifndef NDEBUG
                                               ,
                                               OrigIStr
#endif
    );
    currentInst = Result;
    MadeChange = true;
  }

  // Eliminate copies created that this SILCombine iteration may have
  // introduced during OSSA-RAUW.
  canonicalizeOSSALifetimes(currentInst->isDeleted() ? nullptr : currentInst);

  // Builder's tracking list has been accumulating instructions created by the
  // during this SILCombine iteration. To finish this iteration, go through
  // the tracking list and add its contents to the worklist and then clear
  // said list in preparation for the next iteration.
  for (SILInstruction *I : *Builder.getTrackingList()) {
    if (!I->isDeleted()) {
      if (changeTracker)
        changeTracker->recordInsertion(I);
      LLVM_DEBUG(llvm::dbgs()
                 << "SC: add " << *I << " from tracking list to worklist\n");
      Worklist.add(I);
    }
  }
  Builder.getTrackingList()->clear();
}

namespace swift::test {
struct SILCombinerProcessInstruction {
  void operator()(SILCombiner &combiner, SILInstruction *inst,
                  SILCombineCanonicalize &canonicalizer, bool &madeChange) {
    combiner.processInstruction(inst, canonicalizer, madeChange);
  }
};
// Arguments:
// - instruction: the instruction to be processed
// - bool: remove cond_fails
// - bool: enable lifetime canonicalization
// Dumps:
// - the function after the processing is attempted
static FunctionTest SILCombineProcessInstruction(
    "sil_combine_process_instruction",
    [](auto &function, auto &arguments, auto &test) {
      auto inst = arguments.takeInstruction();
      auto removeCondFails = arguments.takeBool();
      auto enableCopyPropagation = arguments.takeBool();
      SILCombiner combiner(test.getPass(), removeCondFails,
                           enableCopyPropagation);
      SILCombineCanonicalize canonicalizer(combiner.Worklist,
                                           *test.getDeadEndBlocks());
      bool madeChange = false;
      SILCombinerProcessInstruction()(combiner, inst, canonicalizer,
                                      madeChange);
      function.dump();
    });
} // end namespace swift::test

namespace swift::test {
// Arguments:
// - instruction: the instruction to be visited
// Dumps:
// - the function after the visitation is attempted
static FunctionTest SILCombineVisitInstruction(
    "sil_combine_visit_instruction",
    [](auto &function, auto &arguments, auto &test) {
      SILCombiner combiner(test.getPass(), false, false);
      auto inst = arguments.takeInstruction();
      combiner.Builder.setInsertionPoint(inst);
      auto *result = combiner.visit(inst);
      if (result) {
        combiner.Worklist.replaceInstructionWithInstruction(inst, result
#ifndef NDEBUG
                                                            ,
                                                            ""
#endif
        );
      }
      function.dump();
    });
} // end namespace swift::test

bool SILCombiner::runOnFunction(SILFunction &F) {
  clear();
  if (changeTracker &&
      !parentTransform->getPassManager()->takeSILCombineRegion(
          &F, importedDirtyBlocks, fullWorklistSize)) {
    importedDirtyBlocks.clear();
    fullWorklistSize = 0;
  }

  bool Changed = false;
  // Perform iterations until we do not make any changes.
  while (true) {
    bool iterationChanged = doOneIteration(F, Iteration);
    Changed |= iterationChanged;
    // A previously safe region can invoke a utility whose mutation scope is
    // unknown. Confirm saturation with the original full scan in that case.
    if (!iterationChanged &&
        !(lastIterationWasRegional && changeTracker->requiresFullScan()))
      break;
    ++Iteration;
  }
  bool fullSaturation = !lastIterationWasRegional;

  // The wrapper's stack/phi/lifetime repairs are deliberately outside this
  // check: they are also outside the original combiner's fixed-point loop.
  if (VerifyChangedRegions && changeTracker && regionalSeeds && !aborted &&
      doOneIteration(F, ++Iteration, /*forceFullScan=*/true)) {
    llvm::errs() << "SILCombine changed-region saturation failed in "
                 << F.getName() << '\n';
    llvm::report_fatal_error("changed-region SILCombine missed a transformation");
  }
  if (PrintChangedRegions)
    llvm::errs() << "silcombine-regions function=" << F.getName()
                 << " full_seeds=" << fullSeeds
                 << " regional_seeds=" << regionalSeeds
                 << " full_instructions=" << fullSeedInstructions
                 << " regional_instructions=" << regionalSeedInstructions
                 << " cross_pass_seeds=" << crossPassSeeds
                 << " cross_pass_instructions=" << crossPassSeedInstructions
                 << " fallback="
                 << (changeTracker && changeTracker->requiresFullScan()
                         ? changeTracker->getFallbackReason() : "none") << '\n';

  // This is a pending claim only. The pass manager commits it after checking
  // that this pass had no invalidation, callee dependency, or wrapper repair.
  // In particular, shadow verification must not make a regional production
  // run appear to have obtained a full saturation of its own.
  if (changeTracker && !Changed && !aborted && fullSaturation &&
      !invalidatedStackNesting && !F.needBreakInfiniteLoops() &&
      !F.needCompleteLifetimes())
    parentTransform->getPassManager()->publishSILCombineSaturation(
        &F, fullWorklistSize);

  if (invalidatedStackNesting) {
    StackNesting::fixNesting(&F);
  }

  assert(TrackingList.empty() && "TrackingList should be fully processed");
  return Changed;
}

void SILCombiner::eraseInstIncludingUsers(SILInstruction *inst) {
  for (SILValue result : inst->getResults()) {
    while (!result->use_empty()) {
      eraseInstIncludingUsers(result->use_begin()->getUser());
    }
  }
  eraseInstFromFunction(*inst);
}

/// Runs a Swift instruction pass.
void SILCombiner::runSwiftInstructionPass(SILInstruction *inst,
                              void (*runFunction)(BridgedInstructionPassCtxt)) {
  auto invocation = parentTransform->getPassManager()->getSwiftPassInvocation()->getCurrent();
  invocation->startInstructionPassRun(inst);
  runFunction({ {inst->asSILNode()}, {invocation} });
  invocation->finishedInstructionPassRun();
}

/// Registered briged instruction pass run functions.
static llvm::StringMap<BridgedInstructionPassRunFn> swiftInstPasses;
static bool passesRegistered = false;

// Called from initializeSwiftModules().
void SILCombine_registerInstructionPass(BridgedStringRef instClassName,
                                        BridgedInstructionPassRunFn runFn) {
  swiftInstPasses[instClassName.unbridged()] = runFn;
  passesRegistered = true;
}

#define _RUN_SWIFT_SIMPLIFICATON(INST)                                         \
  static BridgedInstructionPassRunFn runFunction = nullptr;                    \
  static bool passDisabled = false;                                            \
  if (!runFunction) {                                                          \
    runFunction = swiftInstPasses[#INST];                                      \
    if (!runFunction) {                                                        \
      if (passesRegistered) {                                                  \
        ABORT([&](auto &out) {                                                 \
          out << "Swift pass " << #INST << " is not registered";               \
        });                                                                    \
      } else {                                                                 \
        return nullptr;                                                        \
      }                                                                        \
    }                                                                          \
    StringRef instName = getSILInstructionName(SILInstructionKind::INST);      \
    passDisabled = SILPassManager::isInstructionPassDisabled(instName);        \
  }                                                                            \
  if (passDisabled &&                                                          \
      SILPassManager::disablePassesForFunction(inst->getFunction())) {         \
    return nullptr;                                                            \
  }                                                                            \
  runSwiftInstructionPass(inst, runFunction);                                  \
  return nullptr;

#define INSTRUCTION_SIMPLIFICATION(INST) \
SILInstruction *SILCombiner::visit##INST(INST *inst) {                     \
  _RUN_SWIFT_SIMPLIFICATON(INST)                                           \
}                                                                          \

#define INSTRUCTION_SIMPLIFICATION_WITH_LEGACY(INST) \
SILInstruction *SILCombiner::visit##INST(INST *inst) {                     \
  if (auto *result = legacyVisit##INST(inst))                              \
    return result;                                                         \
  if (!inst->isDeleted()) {                                                \
    _RUN_SWIFT_SIMPLIFICATON(INST)                                         \
  }                                                                        \
  return nullptr;                                                          \
}                                                                          \

#include "Simplifications.def"

#undef _RUN_SWIFT_SIMPLIFICATON

//===----------------------------------------------------------------------===//
//                                Entry Points
//===----------------------------------------------------------------------===//

namespace {

class SILCombine : public SILFunctionTransform {

  /// The entry point to the transformation.
  void run() override {
    bool enableCopyPropagation =
        getOptions().CopyPropagation >= CopyPropagationOption::Optimizing;
    enableCopyPropagation =
        getOptions().CopyPropagation != CopyPropagationOption::Off;

    SILCombiner Combiner(this, getOptions().RemoveRuntimeAsserts,
                         enableCopyPropagation);
    bool Changed = Combiner.runOnFunction(*getFunction());

    if (Changed) {
      updateAllGuaranteedPhis(getPassManager(), getFunction());
      // Invalidate everything.
      invalidateAnalysis(SILAnalysis::InvalidationKind::FunctionBody);
    }
    if (Changed ||
        (getPassManager()->getSwiftPassInvocation()->getChangeNotifications() & SILContext::Branches) != 0) {
      removeUnreachableBlocks(*getFunction());
      if (getFunction()->needBreakInfiniteLoops())
        breakInfiniteLoops(getPassManager(), getFunction());
      if (getFunction()->needCompleteLifetimes())
        completeAllLifetimes(getPassManager(), getFunction());
    }
  }
};

} // end anonymous namespace

SILTransform *swift::createSILCombine() {
  return new SILCombine();
}

//===----------------------------------------------------------------------===//
//                          SwiftFunctionPassContext
//===----------------------------------------------------------------------===//

void SwiftPassInvocation::eraseInstruction(SILInstruction *inst, bool salvageDebugInfo) {
  if (silCombiner) {
    silCombiner->eraseInstFromFunction(*inst, /*addOperandsToWorklist=*/ true, salvageDebugInfo);
  } else {
    if (inst->isStaticInitializerInst()) {
      // Ignore salvageDebugInfo: Static initializers can't have debug values.
      inst->getParent()->erase(inst, *getPassManager()->getModule());
    } else if (salvageDebugInfo) {
      swift::salvageDebugInfo(inst);
      // Erase debug instructions left behind by salvageDebugInfo.
      // TODO: Change this back to eraseFromParent when salvageDebugInfo is complete.
      swift::eraseFromParentWithDebugInsts(inst);
    } else {
      inst->eraseFromParent();
    }
  }
}
