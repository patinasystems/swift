// RUN: %target-typecheck-verify-swift -debug-constraints 2>%t.debug
// RUN: %target-typecheck-verify-swift -debug-constraints -solver-disable-splitter 2>%t.unsplit.debug
// RUN: %target-typecheck-verify-swift -debug-constraints -solver-enable-diagnose-valid-salvage 2>%t.salvage.debug

// Debug solving verifies that the maintained disjunction worklist has exactly
// the membership and order of the original inactive constraint list. Exercise
// activation, failed choices, connected components, and conjunction snapshots.

func overloaded(_ value: Int) -> Int { value }
func overloaded(_ value: Int) -> String { String(value) }
func overloaded(_ value: String) -> String { value }

func apply<Input, Output>(_ input: Input, _ body: (Input) -> Output) -> Output {
  body(input)
}

func nestedClosures() {
  let integer: Int = apply(1) { value in
    apply(value) { overloaded($0) }
  }
  let string: String = apply(2) { value in
    apply(value) { overloaded($0) }
  }
  let _: (Int, String, String) =
    (overloaded(integer), overloaded(integer), overloaded(string))
}

@resultBuilder
enum WorklistBuilder {
  static func buildExpression<T>(_ expression: T) -> T { expression }
  static func buildBlock<A, B>(_ first: A, _ second: B) -> (A, B) {
    (first, second)
  }
}

func build<Result>(@WorklistBuilder _ body: () -> Result) -> Result {
  body()
}

func conjunctions() {
  let _: (Int, String) = build {
    overloaded(1) as Int
    overloaded(2) as String
  }
  let _: ((Int, String), (String, Int)) = build {
    build {
      overloaded(3) as Int
      overloaded(4) as String
    }
    build {
      overloaded(5) as String
      overloaded(6) as Int
    }
  }
}

func ambiguousChoice(_ value: Int) -> Int { value } // expected-note {{found this candidate}}
func ambiguousChoice(_ value: Int) -> String { String(value) } // expected-note {{found this candidate}}

let ambiguousValue = ambiguousChoice(1) // expected-error {{ambiguous use of 'ambiguousChoice'}}
