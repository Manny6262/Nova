# Security and privacy boundaries

Keep this personal app's services on loopback. Do not expose them to the internet
or treat the Flask development host as a protected production service. Tool execution
uses the current user's privileges. Review any task and any Python check before
granting its scope; audit hooks do not provide an operating-system security boundary.

Do not commit chats, memory databases, task snapshots, credentials, phone numbers,
recordings, screenshots, or model weights. `.gitignore` protects the known runtime
paths. `PUBLIC_FILES.json` is the explicit source-export list; review additions
before updating it. `scripts/check_public_source.py` checks common artifact and
credential patterns without printing matched secret values. It is not an exhaustive
secret scanner.

Use a dedicated secret scanner such as Gitleaks and enable GitHub push protection
for the repository. GitHub's [sensitive-data guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)
explains why a new commit deleting a secret does not remove it from older history.

If reporting a vulnerability publicly, include a synthetic reproduction. Do not
paste a real token, local database, private task snapshot, or another person's data.
