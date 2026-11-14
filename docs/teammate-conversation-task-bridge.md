# Teammate conversation task bridge

Converting a saved teammate conversation requires an explicit user confirmation with a title, description, and acceptance criteria. The API locks the conversation row, reads only messages from its existing session, and keeps the task, graph, handoff, approval, and conversation linkage in the same tenant/project/session boundary.

The conversation becomes a durable task root with one snapshot node. The executable work is the linked child `TaskHandoff`: it uses the existing idempotency key, owner/admin approval, handoff lease, teammate workspace permissions, and task review APIs. The graph is a task/review envelope; it is not a normal workflow-template graph and must not be resumed by the workflow executor. Terminal child state is copied to the root and graph for task-list visibility.

The confirmed description and acceptance criteria are the only instructions in the handoff. Saved user/assistant transcript text is enclosed as untrusted reference evidence and explicitly cannot grant permissions or override the confirmed task.

The handoff API commits the root and approval before acceptance criteria are written. A retry with the same confirmed details repairs missing criteria under row locks; a retry with different details fails with `409`. The deterministic conversation handoff key identifies the one conversion handoff; generic additional handoffs from its root are rejected, and lifecycle synchronization ignores unrelated children. The bounded conversation context is stored in the handoff input and is sized against the existing 24 KB serialized handoff limit before durable task creation. After approval, the graph remains `pending` until the user starts the handoff so generic workflow recovery cannot claim the envelope.
