# Cloud Run console stills

Drop `.png` files here and the `cloud` beat shows them, in filename order,
inside the seconds that beat already has. Leave it empty and the beat is the
live health route and the `.run.app` address, which is what carries the claim
either way.

These are the one asset in the recording a clone cannot regenerate. The console
needs a signed-in session and the video workflow holds none, so they are taken
by hand. Everything else on film is produced by the run that produces it, which
is why they are corroboration rather than evidence.

What is worth capturing, in this order:

1. **Cloud Run → archon → Revisions.** The strongest single frame: the serving
   revision at 100% traffic, the service account that deployed it (no keys
   anywhere), the container image, and the 600s request timeout.
   `https://console.cloud.google.com/run/detail/us-central1/archon/revisions?project=upgradegr-archon-agentic`
2. **Cloud Run → archon → Observability → Metrics.** Real request counts and
   latencies, so the service is visibly serving rather than merely deployed.
   `https://console.cloud.google.com/run/detail/us-central1/archon/observability/metrics?project=upgradegr-archon-agentic`
3. **Cloud Run → archon → Observability → Logs.** Requests arriving at the
   `.run.app` host.
   `https://console.cloud.google.com/run/detail/us-central1/archon/observability/logs?project=upgradegr-archon-agentic`

Capture at 1920x1080 if you can; anything narrower is scaled to fit rather than
cropped, so nothing is lost but the sharpness. Check the frame for anything you
would not put in a public video before committing it.
