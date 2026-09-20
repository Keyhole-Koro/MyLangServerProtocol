# MyLangServerProtocol

A lightweight Language Server Protocol implementation for MyLang.

## Current features

- semantic tokens
- document symbols
- `/** ... */`, `///`, `@param`, and `@return` function documentation
- vermilion `docTag` semantic highlighting for documentation annotations
- Hover for function declarations, resolved calls, and call arguments
- Signature Help with nested-call-aware active parameter selection
- go-to-definition for functions, methods, structs, enums, types, and enum members
- local and relative-import function documentation lookup
- generic declarations, instantiations, and named-imported templates are
  syntax-checked without treating their angle brackets as relational operators

Generic type arguments are exposed as `type` semantic tokens. This includes
container code such as `Vec<Node>` and calls such as `vec_init<i32>(...)`.
The server resolves relative `.mln` imports for Hover, Signature Help, and
go-to-definition. Completion is not implemented yet.

Source locations from the native syntax checker are converted from UTF-8 byte
columns to the UTF-16 positions used by LSP clients.

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

The package can also be launched directly with:

```bash
PYTHONPATH=src python3 -m mylang_lsp
```

## Tests

```bash
for test_file in tests/test_*.py; do python3 "$test_file" || exit 1; done
```
