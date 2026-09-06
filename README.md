# MyLangServerProtocol

A lightweight Language Server Protocol implementation for MyLang.

## Current features

- semantic tokens
- document symbols
- generic declarations, instantiations, and named-imported templates are
  syntax-checked without treating their angle brackets as relational operators

Generic type arguments are exposed as `type` semantic tokens. This includes
container code such as `Vec<Node>` and calls such as `vec_init<i32>(...)`.
The server does not yet resolve imported modules for hover or go-to-definition.

## Run manually

```bash
python3 server.py
```

The server speaks JSON-RPC 2.0 over stdio.
