# MyLangServerProtocol

A lightweight Language Server Protocol implementation for MyLang.

## Current features

- semantic tokens
- document symbols
- `/** ... */`, `///`, `@param`, and `@return` function documentation
- vermilion `docTag` semantic highlighting for documentation annotations
- Hover for function declarations, resolved calls, call arguments, and struct
  declarations or type references (including imported structs)
- Signature Help with nested-call-aware active parameter selection
- go-to-definition for functions, methods, structs, enums, types, and enum
  members, with a target picker when more than one declaration matches
- find references for declarations, calls, and function-pointer uses
- traits: impl items resolve as call targets (`Self` receivers read as the
  impl's type, `Type::from(...)` reaches associated functions), Hover on a
  trait shows its items, doc, and known impls, an impl item without its own
  doc shows the trait item's, and go-to-implementation lists a trait's impls
  or a trait item's impl items; generic impls (`impl<T: Eq> Eq for Box<T>`)
  and default items are covered too
- `.model.mln` files: see below
- local and relative-import function documentation lookup
- generic declarations (including `const N` capacity parameters),
  instantiations, and named-imported templates are
  syntax-checked without treating their angle brackets as relational operators

Generic type arguments are exposed as `type` semantic tokens. This includes
container code such as `Vec<Node>` and calls such as `vec_init<i32>(...)`;
numeric const arguments such as the `128` in `Text<128>` remain number
tokens. Go-to-definition resolves a const parameter use such as `data[N]` to
its lexical `const N` declaration.
The server resolves relative `.mln` imports and `@alias/...` imports (through
the nearest `mylang.toml`'s `[alias]` table) for Hover, Signature Help, and
go-to-definition. Completion is not implemented yet.

Source locations from the native syntax checker are converted from UTF-8 byte
columns to the UTF-16 positions used by LSP clients.

## Model files

A `.model.mln` file declares entity tables that the compiler expands to
ordinary MyLang (`docs/design/mylang-model.md` in MyComputer). The expansion is
what the server analyses, so the generated functions are found like any others:

- diagnostics are the generator's own checks, one per bad statement, on the
  word that is wrong, plus syntax errors in the lines that pass through
- `entity`, `max`, `has`, `knows`, `many`, `one`, `as`, `ordered`, `index`,
  `unique`, entity names and relation names are highlighted; a column is
  highlighted exactly as a struct field is
- go-to-definition from a generated function lands on the word that produced
  it: the entity's name for a table function, the relation's name (or its
  inverse's) for a relation function, the column's name for a column accessor
  or index; `.kind` after `row()` lands on the column too
- Hover shows the generated signature and doc comment, in a file that uses it
  and on its declaration in the model
- find references on an entity or a relation in the model covers everything
  generated from it, under any name, in any file; find references on one
  generated name covers that name and its declaration
- document symbols list entities and relations, not the generated functions

The generator's naming rules live only in the compiler. `mylang-syntax-check
--stdio` answers a `model <length>` record with two lines of JSON: the trace
from `model_trace()` (`model_expand.h`), and the usual result for its text.
That text is the file with every word still on its own position, then the
generated code, so a token of the file needs no mapping and a generated line
maps back through one origin in the trace. `AnalysisUnit.model` holds that map.

## Run manually

```bash
python3 server.py
```

The server speaks JSON-RPC 2.0 over stdio.

## Structure

- `server.py`: stable executable wrapper used by the editor extension
- `lsp_analysis.py`: compatibility exports for existing integrations
- `src/mylang_lsp/protocol/`: JSON-RPC/LSP lifecycle, scheduling, and dispatch
- `src/mylang_lsp/analysis/`: document snapshots, shared models, frontend
  metadata, Hover, Signature Help, and go-to-definition
- `src/mylang_lsp/features/`: semantic tokens, diagnostics, and document
  symbols
- `src/mylang_lsp/frontend/`: native syntax-checker process and result cache
- `tests/support/`: fixture, temporary-workspace, and in-memory LSP test tools
- `tests/fixtures/`: MyLang and MLX source fixtures grouped by feature
- `tests/frontend/`, `tests/interaction/`, `tests/navigation/`, and
  `tests/protocol/`: feature-oriented test modules

The package can also be launched directly with:

```bash
PYTHONPATH=src python3 -m mylang_lsp
```

## Tests

```bash
python3 -m tests.run
```
