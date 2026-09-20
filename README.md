# MyLangServerProtocol

A lightweight Language Server Protocol implementation for MyLang.

## Current features

- semantic tokens
- document symbols
- `/** ... */`, `///`, `@param`, and `@return` function documentation
- vermilion `docTag` semantic highlighting for documentation annotations
- Hover for function declarations, resolved calls, and call arguments
- Signature Help with nested-call-aware active parameter selection
- local and relative-import function documentation lookup
- generic declarations, instantiations, and named-imported templates are
  syntax-checked without treating their angle brackets as relational operators

Generic type arguments are exposed as `type` semantic tokens. This includes
container code such as `Vec<Node>` and calls such as `vec_init<i32>(...)`.
The server resolves relative `.mln` imports for Hover and Signature Help.
Go-to-definition and Completion are not implemented yet.

Source locations from the native syntax checker are converted from UTF-8 byte
columns to the UTF-16 positions used by LSP clients.

## Run manually

```bash
python3 server.py
```

The server speaks JSON-RPC 2.0 over stdio.

## Tests

```bash
for test_file in tests/test_*.py; do python3 "$test_file" || exit 1; done
```
