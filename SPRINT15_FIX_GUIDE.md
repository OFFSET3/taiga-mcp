# Sprint 15 Story Fixes - Execution Guide

## Overview

This guide provides step-by-step instructions for fixing Sprint 15 stories using the Taiga MCP Server's HTTP Actions API.

---

## Prerequisites

1. **API Key**: Access to `action-proxy-api-key` from Azure Container Apps
2. **Python**: Python 3.11+ with `httpx` package installed
3. **Network**: Access to Taiga MCP Server at `https://taiga-mcp.politeground-c43f6662.eastus.azurecontainerapps.io`

---

## Step 1: Get API Key from Azure

### Option A: Azure Portal (Web UI)

1. Open [Azure Portal](https://portal.azure.com)
2. Navigate to: **Container Apps** → **taiga-mcp**
3. In left menu, click: **Settings** → **Secrets**
4. Find secret: `action-proxy-api-key`
5. Click **Show value** button (eye icon)
6. Copy the API key value

### Option B: Azure CLI

```bash
# Login to Azure
az login

# Get the secret value
az containerapp secret show \
  --name taiga-mcp \
  --resource-group <resource-group-name> \
  --secret-name action-proxy-api-key \
  --query value -o tsv
```

### Option C: PowerShell (Azure PowerShell Module)

```powershell
# Login to Azure
Connect-AzAccount

# Get the secret value
$secret = Get-AzContainerAppSecret `
  -Name taiga-mcp `
  -ResourceGroupName <resource-group-name> `
  -SecretName action-proxy-api-key

$secret.Value
```

---

## Step 2: Set Environment Variable

### Windows (PowerShell)

```powershell
# Set for current session
$env:ACTION_PROXY_API_KEY = "your-api-key-here"

# Verify it's set
$env:ACTION_PROXY_API_KEY
```

### Linux/macOS (Bash)

```bash
# Set for current session
export ACTION_PROXY_API_KEY="your-api-key-here"

# Verify it's set
echo $ACTION_PROXY_API_KEY
```

### Alternative: Save to Config File

The script can also read from `~/.taiga-mcp/api-key.txt`:

```powershell
# Windows PowerShell
$keyDir = "$env:USERPROFILE\.taiga-mcp"
New-Item -ItemType Directory -Force -Path $keyDir
Set-Content -Path "$keyDir\api-key.txt" -Value "your-api-key-here"
```

```bash
# Linux/macOS
mkdir -p ~/.taiga-mcp
echo "your-api-key-here" > ~/.taiga-mcp/api-key.txt
chmod 600 ~/.taiga-mcp/api-key.txt
```

---

## Step 3: Install Dependencies

```bash
# Navigate to taiga-mcp directory
cd taiga-mcp

# Install httpx if not already installed
pip install httpx
```

---

## Step 4: Run the Fix Script

```bash
# Execute the fix script
python fix_sprint15_final.py
```

### Expected Output

```
======================================================================
Sprint 15 Stories - Final Fix (HTTP Actions API)
======================================================================

✅ Using API key from ACTION_PROXY_API_KEY environment variable

📋 Fix Plan:
1. Link stories #355, #356, #358 to Epic #186
2. Delete duplicate Story #357
3. Create missing Story (Taiga Truth Sync)
4. Verify all epic links

======================================================================
STEP 1: Linking Stories to Epic #186
======================================================================

📎 Linking #355 - Context Evaluator Gate to Epic #186...
✅ Successfully linked #355 - Context Evaluator Gate

📎 Linking #356 - VFS Metadata Contract to Epic #186...
✅ Successfully linked #356 - VFS Metadata Contract

📎 Linking #358 - Manifest-First Retrieval to Epic #186...
✅ Successfully linked #358 - Manifest-First Retrieval

======================================================================
STEP 2: Deleting Duplicate Story
======================================================================

🗑️  Deleting #357 - Duplicate...
✅ Successfully deleted #357 - Duplicate

======================================================================
STEP 3: Creating Missing Story
======================================================================

➕ Creating story: [A-13] — Taiga Truth Sync to Memory...
✅ Successfully created story #359 (ID: 8781374)
📎 Linking #359 to Epic #186...
✅ Successfully linked #359

======================================================================
STEP 4: Verification
======================================================================

🔍 Verifying epic links...

📊 Sprint 15 Stories Status:
======================================================================
Story #355 (ID: 8781351): [A-13] — Context Evaluator Gate for Memory Promotion
  ✅ Epic #186

Story #356 (ID: 8781370): [A-13] — VFS Context Metadata Contract
  ✅ Epic #186

Story #358 (ID: 8781373): [A-13] — Manifest-First Context Retrieval
  ✅ Epic #186

Story #359 (ID: 8781374): [A-13] — Taiga Truth Sync to Memory
  ✅ Epic #186

======================================================================
SUMMARY
======================================================================
Stories linked to epic: 3/3
Duplicate deleted: ✅ Yes
Missing story created: ✅ Yes

Total Success Rate: 5/5 operations

🎉 All Sprint 15 story fixes completed successfully!
```

---

## Step 5: Verify in Taiga

1. Open [Taiga Project](https://tree.taiga.io/project/johnwblack-aresnet/)
2. Navigate to **Epic #186** (A-13 — Builder Agent)
3. Verify the following stories are linked:
   - **Story #355**: Context Evaluator Gate for Memory Promotion (8 pts)
   - **Story #356**: VFS Context Metadata Contract (5 pts)
   - **Story #358**: Manifest-First Context Retrieval (5 pts)
   - **Story #359**: Taiga Truth Sync to Memory (3 pts)
4. Verify **Story #357** (duplicate) has been deleted
5. Total Sprint 15 Points: **21 points**

---

## Troubleshooting

### Error: 401 Unauthorized

**Problem**: API key is incorrect or not set

**Solution**:
1. Verify API key is correct from Azure Portal
2. Check environment variable is set: `echo $env:ACTION_PROXY_API_KEY` (PowerShell)
3. Ensure no extra spaces or quotes in the API key value

### Error: 403 Forbidden

**Problem**: API key is valid but lacks permissions

**Solution**:
1. This should NOT happen with HTTP Actions API (different from Python client)
2. Verify you're using `fix_sprint15_final.py` (not the old client-based scripts)
3. Check Taiga MCP Server logs in Azure Container Apps

### Error: 404 Not Found

**Problem**: Story ID or endpoint URL is incorrect

**Solution**:
1. Verify story IDs in the script match Taiga story IDs
2. Check MCP_BASE_URL is correct: `https://taiga-mcp.politeground-c43f6662.eastus.azurecontainerapps.io`
3. Ensure `/actions/` endpoints are deployed in Taiga MCP Server

### Error: Connection Timeout

**Problem**: Network connectivity or server availability

**Solution**:
1. Check internet connection
2. Verify Taiga MCP Server is running in Azure
3. Increase timeout in script: `httpx.AsyncClient(timeout=60.0)`

---

## Rollback Plan

If the fix script fails partway through:

1. **Epic Links**: Run verification to see which stories were successfully linked
2. **Deleted Story**: Cannot be undeleted (ensure #357 is truly a duplicate first)
3. **Created Story**: Can be manually deleted if creation was incorrect

To manually link a story to an epic:
```bash
curl -X POST \
  https://taiga-mcp.politeground-c43f6662.eastus.azurecontainerapps.io/actions/add_story_to_epic \
  -H "X-Api-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"epic_id": 186, "story_id": 8781351}'
```

---

## Security Notes

- **Never commit API keys** to Git repositories
- **Store API key securely** in environment variables or config files (with restricted permissions)
- **Rotate API keys** regularly (recommended: every 90 days)
- **Audit trail**: All HTTP Actions are logged in Taiga MCP Server

---

## Next Steps After Fix

1. ✅ Verify all stories in Taiga UI
2. ✅ Update Sprint 15 board with correct story points (21 total)
3. ✅ Begin implementing Sprint 15 stories (VFS enhancements)
4. ✅ Update Builder Agent to use HTTP Actions API (avoid Python client issues)

---

## Related Documentation

- [MCP API Coverage Analysis](MCP_API_COVERAGE_ANALYSIS.md) - Why HTTP Actions work vs Python client
- [Sprint 15 Analysis](SPRINT15_ANALYSIS.md) - Detailed issue breakdown
- [Sprint 15 Fix Summary](SPRINT15_FIX_SUMMARY.md) - Comprehensive fix documentation
- [Taiga MCP Server README](README.md) - API documentation and usage

---

**Last Updated**: 2025-12-09  
**Author**: OFFSET3 AI Engineering Team  
**Status**: Ready for execution
