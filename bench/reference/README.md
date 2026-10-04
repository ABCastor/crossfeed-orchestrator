The generator writes a completed reference into each task's `reference/` directory. `reference/workspace/` holds the fixture files and `reference/reply.txt` holds the reply. Keep both outside a model's workspace and prompt.

To copy a reference into an empty directory and print its reply:

```sh
python3 -m bench.reference TASK_DIR EMPTY_WORKSPACE REPLY_FILE
```

Python callers can use `bench.reference.materialize(task_dir, workspace_dir, reply_file)`. This helper tests grid plumbing offline; it requires the hidden task directory and does not belong in a real model comparison.

Every family requires a nonempty reply. Review, repo QA and reasoning read JSON from the final nonempty line. Review uses new-file line numbers with tolerance one and accepts a leading `./` on the file path. QA and reasoning string answers ignore case, outer whitespace, and whitespace adjacent to commas or colons. Numeric answers require integers. Extraction checks required fields recursively with exact types and values, and permits extra fields.

Coding checks run trusted tests against a temporary copy with a ten-second limit. Fix tests must be byte-identical, with no test files added or deleted. Hidden tests never persist into the submitted workspace. A second interpreter computes results from inputs alone; the parent checker compares their types, values, and unchanged arguments against hidden expectations. Disabling unittest assertions cannot bypass that comparison.

The temporary copy is not a security sandbox: submitted Python executes locally. The runner must apply the process and filesystem permissions needed for untrusted commands.
