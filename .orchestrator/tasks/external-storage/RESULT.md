IMPLEMENTED; independent review pending.

- Added --materials-dir / SSU_LMS_MATERIALS_DIR with CLI > environment > data-dir/materials precedence.
- Downloads, document listing, in-root relative/absolute reads, and search share the configured root. Existing root-level and named-subfolder documents are included.
- Session and manifest remain in data_dir. Cached paths carry storage_root; legacy paths only apply to the default root. LTI attempt history is preserved.
- Added storage_status MCP tool and README configuration/restart instructions. No real user destination configured, existing files moved, or account settings changed.
- Verification: 159 pytest passed, 1 Windows symlink-privilege skip; ruff passed; wheel/sdist build passed; git diff --check passed (project source is untracked in parent Git, so ownership baseline is the source-change audit).
- Ownership audit: all source/test/document edits are within the explicit file set; raw check reports five pre-existing Python bytecode cache files changed outside ownership. They were not deleted or reset. No out-of-scope source edits reported.
- Graphify refreshed successfully: 327 nodes, 940 edges.
