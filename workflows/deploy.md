# Deploy Workflow

> BotBot deployment to norisor (Tailscale: 100.66.170.31)

## Deploy Ceremony (NEVER SKIP)

Every deploy follows these 5 steps in order. No shortcuts, no "it's a small change."

### Steps

1. **Implement and test locally**
   - Code complete, runs locally without errors
   - Command: `docker compose build brain openclaw && docker compose up -d`
   - Verify: `curl -s http://localhost:8400/health` returns `{"status":"ok"}`

2. **Run project checks/linters — PASS**
   - Python compile check: `python3 -m py_compile brain/src/*.py`
   - State validation: `python3 taskmaster.py validate`

3. **Manual test in browser/UI**
   - WebChat: `http://localhost:18789/?token=botbot-dev`
   - Brain API: `http://localhost:8400/health`

4. **Explicit user approval**
   - User says "OK, deploy" in chat. No exceptions.
   - Never deploy from own initiative.

5. **Execute deploy**
   ```bash
   # Push code
   git push origin main

   # Deploy on norisor
   ssh norisor "cd ~/botbot && git pull && docker compose build brain openclaw && docker compose up -d"
   ```

6. **Run migrations (if any)**
   ```bash
   # Apply new migrations (check brain/migrations/ for unapplied files)
   ssh norisor "cd ~/botbot && docker compose exec postgres psql -U agent -d agent_memory -f /dev/stdin < brain/migrations/XXX.sql"
   ```
   Note: schema.sql runs on brain startup and covers most DDL idempotently. Explicit migration step only needed for data migrations or ALTER TABLE.

### Forbidden

- Deploy without steps 1-4 completed
- Skipping local test "because it's a small change"
- Deploying from own initiative without explicit user approval
- Running DELETE/TRUNCATE/DROP on production DB without explicit user approval
- Modifying .env on norisor without telling the user

### Post-Deploy Verification

After deploy, verify the change is live and working:

- [ ] Brain healthy: `ssh norisor "curl -s http://localhost:8400/health"`
- [ ] OpenClaw reachable: `ssh norisor "curl -s -o /dev/null -w '%{http_code}' http://localhost:18789/"`
- [ ] Postgres healthy: `ssh norisor "docker compose -f ~/botbot/docker-compose.yml ps postgres"`
- [ ] No crash loops: `ssh norisor "docker compose -f ~/botbot/docker-compose.yml logs brain --tail 5"`

### Production Safety

- Production DB (on norisor) should be treated with care. No bulk DELETE without approval.
- `.env` contains API keys — never commit, never print in logs.
- Brain state volume (`brain-state`) persists across restarts. Back up before destructive changes.
- Postgres data volume (`pgdata`) persists across restarts. Back up before schema-breaking migrations.

## Environment Details

| Environment | Host | Access |
|-------------|------|--------|
| Local | tuf-1 (this machine) | `http://localhost:8400` (brain), `http://localhost:18789` (webchat) |
| Production | norisor (100.66.170.31 via Tailscale) | `ssh norisor`, then same ports. WebChat: `http://norisor:18789/?token=botbot-dev` |

## Deploy Command (copy-paste ready)

```bash
# Full deploy sequence (from tuf-1)
git push origin main && \
ssh norisor "cd ~/botbot && git pull && docker compose build brain openclaw && docker compose up -d" && \
sleep 10 && \
ssh norisor "curl -s http://localhost:8400/health"
```

## First-Time Setup (already done)

```bash
# 1. Clone repo
ssh norisor "git clone git@github.com:stonks-git/botbot.git ~/botbot"

# 2. Create .env
ssh norisor "cat > ~/botbot/.env << 'EOF'
GOOGLE_API_KEY=<key>
ANTHROPIC_API_KEY=placeholder
EOF"

# 3. Build and start
ssh norisor "cd ~/botbot && docker compose build && docker compose up -d"
```

## Stopping / Starting

```bash
# Stop all services (keeps data)
ssh norisor "cd ~/botbot && docker compose stop"

# Start all services
ssh norisor "cd ~/botbot && docker compose up -d"

# Full teardown (keeps volumes/data)
ssh norisor "cd ~/botbot && docker compose down"

# Nuclear teardown (DELETES ALL DATA)
ssh norisor "cd ~/botbot && docker compose down -v"
```
