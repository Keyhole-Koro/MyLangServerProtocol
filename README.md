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

- `server.py`: JSON-RPC/LSP lifecycle, scheduling, and request dispatch
- `document_model.py`: immutable document snapshots and position conversion
- `native_frontend.py`: native syntax-checker process and result cache
- `analysis_model.py`: shared analysis data and workspace index
- `frontend_analysis.py`: syntax-checker output to editor metadata
- `language_features.py`: Hover, Signature Help, and go-to-definition
- `semantic_tokens.py`, `diagnostics.py`, `document_symbols.py`: individual
  LSP feature services
- `lsp_analysis.py`: compatibility exports for existing integrations

## Tests

```bash
for test_file in tests/test_*.py; do python3 "$test_file" || exit 1; done
```
