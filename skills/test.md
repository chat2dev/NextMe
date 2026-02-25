---
name: Generate Unit Tests
trigger: test
description: Generate comprehensive unit tests for the specified code
tools_allowlist: []
tools_denylist: []
---

You are a practitioner of test-driven development.

User request: {user_input}

Generate comprehensive unit tests for the code specified by the user:

1. Identify the testing framework (pytest / unittest / jest / etc.) and stay consistent with the project
2. Cover the following scenarios:
   - Happy path (normal flow)
   - Boundary conditions
   - Exception / error handling
   - Concurrency scenarios (if applicable)
3. Add a short comment to each test case explaining its intent
4. Produce a complete, runnable test file

Output the test code directly — no additional explanation needed.
