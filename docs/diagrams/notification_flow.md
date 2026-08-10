# Notification Flow

One notification's life, from control loop to endpoint.

The two diagrams below run on **different threads**. Everything in the first runs on the
controller thread and performs no I/O; everything in the second runs on the notifier's own worker
thread. The bounded queue is the only thing that crosses between them, and the only thing handed
across is an immutable snapshot.

## Scheduling — controller thread

```mermaid
flowchart TD

A[Controller Tick] --> B{Any enabled notifier?}

B -- No --> Z[Return to control loop]

B -- Yes --> C[Build immutable Notification snapshot from State]

C --> D[For each notifier]

D --> E{Trigger.is_active?}

E -- No --> F[Reset rising edge]
F --> Z

E -- Yes --> G{Rising edge<br/>or Interval elapsed?}

G -- No --> Z

G -- Yes --> H[Filter snapshot to this notifier's sensors]

H --> I{Queue full?}

I -- Yes --> J[Discard oldest job<br/>log warning]
J --> K[Enqueue job<br/>advance next_due]

I -- No --> K

K --> Z
```

A rising edge — the trigger becoming active after being inactive — always fires immediately.
`next_due` is advanced only here, so delivery outcome and retries never shift a notifier's
schedule.

## Delivery — worker thread

```mermaid
flowchart TD

W[Worker thread] --> X{Stop requested?}

X -- Yes --> Y[Discard pending jobs<br/>exit thread]

X -- No --> Q[Dequeue job<br/>with timeout]

Q --> S[Endpoint.send]

S --> R{Result}

R -- Success --> T[Debug log:<br/>notifier, endpoint, timestamp, result]

R -- TransientEndpointError --> U{Attempts remaining?}

R -- PermanentEndpointError --> V[Discard job<br/>log warning]

U -- Yes --> P[Cancellable backoff<br/>capped at 30s]
P --> S

U -- No --> V

T --> X
V --> X
```

Transient failures — connection, DNS, TLS, timeout, `5xx`, and `429` — are retried within the
notifier's `MaxAttempts`. Permanent failures, including authentication and authorisation
failures, discard the job on the first attempt rather than burning the retry budget on a request
that will never succeed.

Endpoint failure never deactivates a notifier. After a job is discarded, the worker continues
with the next one.

## Invariants

- The controller thread never performs network I/O.
- Nothing mutable crosses the queue; only a detached, immutable snapshot does.
- Queue capacity, queue overflow, and delivery failure are per notifier. One endpoint's
  saturation or outage cannot consume another's capacity or block its worker.
- Both paths terminate. The queue is bounded, retries are bounded, and backoff is capped.
