# ChatCourier integration guide

Use this repository only for the mechanical ChatGPT transport. Prepare a
request directory, call `chat-courier validate`, then explicitly call
`chat-courier run`. Do not invoke Gmail, a second browser controller, or a
parallel reader for the same request.

The Agent must use a unique `request_id`, keep its own process alive for at
least 420 seconds when ChatCourier uses the default 360-second window, and
read `response.txt` only after the `response_received` event. Do not rerun a
changed request directory: create a new request ID instead.

ChatCourier's prompt marks Agent text as quoted reference. ChatGPT has final
authority over task scope, difficulty, and detail. `task_difficulty` and
`instruction_level` are optional preferences, not commands.

Run only targeted tests explicitly named for the current change. Do not add a
persistent service, mailbox transport, worker scheduler, or browser runtime
outside the single bounded ChatCourier process.
