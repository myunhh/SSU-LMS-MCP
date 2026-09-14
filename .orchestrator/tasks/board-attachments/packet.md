# Board attachments

- Classification: M
- Objective: expose LearningX board posts and attachments, and safely download selected board files.
- Ownership: `src/ssu_lms_mcp/models.py`, `src/ssu_lms_mcp/board.py`, `src/ssu_lms_mcp/server.py`, `src/ssu_lms_mcp/downloads.py`, `tests/test_board.py`, `tests/test_protocol.py`, `README.md`.
- Acceptance: board/API attachment metadata is preserved; selected valid attachments save under `materials`; invalid/HTML/video/oversize responses are rejected; `pytest`, `ruff check src tests`, `build`, and `git diff --check` pass.
- Evidence: live LMS course 48541 board tab 41 contained post 346081 with `OS_01_Introduction_2026.pdf` (1,054KB completed in Chrome).
