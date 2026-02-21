# Deploy Workflow

> Fill this template when the project has a deploy target (server, cloud, container registry, etc.).

## Deploy Ceremony (NEVER SKIP)

Every deploy follows these 5 steps in order. No shortcuts, no "it's a small change."

### Steps

1. **Implement and test locally**
   - Code complete, runs locally without errors
   - Command: `_fill: e.g. python manage.py check, npm run build_`

2. **Run project checks/linters — PASS**
   - All automated checks must pass before proceeding
   - Command: `_fill: e.g. pytest, npm test, cargo check_`

3. **Manual test in browser/UI**
   - Verify the change works as expected by hand
   - Access: `_fill: e.g. http://localhost:8000, http://localhost:3000_`

4. **Explicit user approval**
   - User says "OK, deploy" in chat. No exceptions.
   - Never deploy from own initiative.

5. **Execute deploy command**
   - Command: `_fill: e.g. git push heroku main, docker compose up -d_`

### Forbidden

- Deploy without steps 1-4 completed
- Assuming "it works on my machine" means it works in production
- Skipping local test "because it's a small change"
- Deploying from own initiative without explicit user approval
- `_fill: project-specific forbidden actions_`

### Post-Deploy Verification

After deploy, verify the change is live and working:

- [ ] Service is reachable: `_fill: e.g. curl -I https://example.com_`
- [ ] Key functionality works: `_fill: e.g. login, main flow_`
- [ ] No error spikes in logs: `_fill: e.g. check error tracker_`

### Production Safety

- `_fill: e.g. "Production DB is READ-ONLY for the agent. Only migrate and SELECT allowed."_`
- `_fill: e.g. "Never run destructive commands (TRUNCATE, DELETE, DROP) on production."_`

## Environment Details

| Environment | Host | Access |
|-------------|------|--------|
| Local | `_fill_` | `_fill_` |
| Staging | `_fill_` | `_fill_` |
| Production | `_fill_` | `_fill_` |

## Deploy Command (copy-paste ready)

```bash
# Fill with your actual deploy command sequence
# Example:
# git push origin main && ssh deploy@server "cd /app && git pull && systemctl restart app"
```
