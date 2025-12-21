# Taiga MCP Quick Reference

**Version:** 1.1.0  
**Updated:** December 17, 2024

---

## New MCP Tools

### GET Endpoints

```python
# Get epic with all fields
epic = await session.call_tool("taiga.epics.get", {
    "epic_id": 123
})

# Get story with all fields
story = await session.call_tool("taiga.stories.get", {
    "user_story_id": 456
})

# Get task with all fields
task = await session.call_tool("taiga.tasks.get", {
    "task_id": 789
})
```

---

### Enhanced List Endpoints

```python
# Minimal fields (default - fastest, smallest payload)
epics = await session.call_tool("taiga.epics.list", {
    "project_id": 1
})
# Returns: [{id, ref, subject, created_date, modified_date, status}]

# Full details (includes tags/description)
epics = await session.call_tool("taiga.epics.list", {
    "project_id": 1,
    "include_details": True
})
# Returns: [{id, ref, subject, description, tags, status, ...}]

# Paginated
epics = await session.call_tool("taiga.epics.list", {
    "project_id": 1,
    "page": 1,
    "page_size": 10
})
```

---

### Append-Only Updates (Safe Collaborative Editing)

```python
# Append to description (doesn't overwrite)
result = await session.call_tool("taiga.stories.update", {
    "user_story_id": 123,
    "append_description": "\\n\\nUpdate: New information added by automation"
})

# Merge tags (doesn't overwrite existing)
result = await session.call_tool("taiga.stories.update", {
    "user_story_id": 123,
    "add_tags": ["automated", "needs-review"]
})

# Works for tasks too
result = await session.call_tool("taiga.tasks.update", {
    "task_id": 456,
    "append_description": "Progress update: Completed step 1",
    "add_tags": ["in-progress"]
})
```

---

### DELETE Endpoints

```python
# Hard delete epic (DESTRUCTIVE)
result = await session.call_tool("taiga.epics.delete", {
    "epic_id": 123
})
# Returns: {id: 123, deleted: true}

# Hard delete story (DESTRUCTIVE)
result = await session.call_tool("taiga.stories.delete", {
    "user_story_id": 456
})

# Hard delete task (DESTRUCTIVE)
result = await session.call_tool("taiga.tasks.delete", {
    "task_id": 789
})
```

---

### Soft-Delete (Production-Safe)

```python
# Archive task (closes and tags, preserves history)
result = await session.call_tool("taiga.tasks.archive_or_close", {
    "task_id": 789
})
# Behavior:
# 1. Finds a closed status automatically
# 2. Sets task status to closed
# 3. Adds 'archived-by-mcp' tag
# 4. Returns updated task

# Archive story (same pattern)
result = await session.call_tool("taiga.stories.archive_or_close", {
    "user_story_id": 456
})

# Custom closed status
result = await session.call_tool("taiga.tasks.archive_or_close", {
    "task_id": 789,
    "closed_status": "Closed",  # or status ID
    "add_archive_tag": True     # default
})
```

---

## Common Patterns

### Read-Verify-Write (Safe Updates)

```python
# 1. Read current state
story = await session.call_tool("taiga.stories.get", {
    "user_story_id": 123
})

# 2. Verify conditions
if story["status"] == "Ready":
    # 3. Update with append-only semantics
    result = await session.call_tool("taiga.stories.update", {
        "user_story_id": 123,
        "append_description": f"\\n\\nUpdate on {datetime.now()}: New information",
        "add_tags": ["updated-by-automation"]
    })
```

### Batch Archive Tasks

```python
# Get all tasks for a story
tasks_result = await session.call_tool("taiga.tasks.list", {
    "user_story_id": 123
})

# Archive each task
for task in tasks_result["tasks"]:
    await session.call_tool("taiga.tasks.archive_or_close", {
        "task_id": task["id"]
    })
```

### Pagination Loop

```python
page = 1
all_epics = []

while True:
    epics = await session.call_tool("taiga.epics.list", {
        "project_id": 1,
        "page": page,
        "page_size": 50
    })
    
    if not epics:
        break
    
    all_epics.extend(epics)
    page += 1
```

---

## Action Proxy (HTTP API)

### GET Endpoints

```bash
# Get epic
curl -H "X-API-Key: $API_KEY" \\
  "http://localhost:8000/actions/get_epic?epic_id=123"

# Get task
curl -H "X-API-Key: $API_KEY" \\
  "http://localhost:8000/actions/get_task?task_id=456"

# List epics with details (not implemented in action proxy - use MCP tools)
```

---

## Migration Examples

### Before (Risky - Overwrites)

```python
# Blind update
await session.call_tool("taiga.stories.update", {
    "user_story_id": 123,
    "tags": ["new-tag"]  # Overwrites all existing tags!
})
```

### After (Safe - Merges)

```python
# Merge tags
await session.call_tool("taiga.stories.update", {
    "user_story_id": 123,
    "add_tags": ["new-tag"]  # Merges with existing tags
})
```

---

## Error Handling

```python
try:
    result = await session.call_tool("taiga.stories.update", {
        "user_story_id": 123,
        "description": "Overwrite",
        "append_description": "Append"  # ERROR!
    })
except Exception as e:
    # ValueError: Cannot set both 'description' and 'append_description'
    print(f"Error: {e}")
```

**Common Errors:**
- Cannot set both `description` and `append_description`
- Cannot set both `tags` and `add_tags`
- Version conflict (409): Use GET to fetch latest version
- Not found (404): Object doesn't exist
- Permission denied (403): Check API key

---

## Best Practices

### 1. Use Append-Only for Collaborative Edits
```python
# ✅ Good: Preserves existing content
append_description="New info"

# ❌ Risky: Overwrites everything
description="New info"
```

### 2. Use Soft-Delete for Production
```python
# ✅ Good: Reversible, audit-friendly
await session.call_tool("taiga.tasks.archive_or_close", {"task_id": 123})

# ❌ Risky: Permanent deletion
await session.call_tool("taiga.tasks.delete", {"task_id": 123})
```

### 3. Use Minimal Fields for Large Lists
```python
# ✅ Good: Small payload (15 KB for 100 epics)
epics = await session.call_tool("taiga.epics.list", {
    "project_id": 1
})

# ⚠️  Caution: Large payload (500 KB for 100 epics)
epics = await session.call_tool("taiga.epics.list", {
    "project_id": 1,
    "include_details": True
})
```

### 4. Use Pagination for Safety
```python
# ✅ Good: Bounded payload size
for page in range(1, 11):  # Max 10 pages
    epics = await session.call_tool("taiga.epics.list", {
        "project_id": 1,
        "page": page,
        "page_size": 50
    })
    if not epics:
        break
```

---

## Troubleshooting

### Issue: Large payload safety blocks

**Solution:** Use pagination and minimal fields
```python
epics = await session.call_tool("taiga.epics.list", {
    "project_id": 1,
    "page": 1,
    "page_size": 10  # Smaller batches
})
```

### Issue: Tags overwritten accidentally

**Solution:** Use `add_tags` instead of `tags`
```python
# Before
"tags": ["new-tag"]  # Overwrites existing

# After
"add_tags": ["new-tag"]  # Merges with existing
```

### Issue: Need to undo deletion

**Solution:** Use soft-delete instead
```python
# Reversible: Change status back, remove tag
await session.call_tool("taiga.tasks.archive_or_close", {"task_id": 123})

# Irreversible: Cannot undo
await session.call_tool("taiga.tasks.delete", {"task_id": 123})
```

---

## Performance Tips

1. **Batch GET operations** when possible (future: bulk GET endpoint)
2. **Use pagination** for lists > 50 items
3. **Cache results** from GET endpoints (objects change slowly)
4. **Minimize fields** returned from lists
5. **Use version numbers** to detect conflicts early

---

## Version Compatibility

| Feature | Version | Backward Compatible |
|---------|---------|---------------------|
| `epics.get` | 1.1.0 | ✅ New tool |
| `stories.get` | 1.1.0 | ✅ New tool |
| `tasks.get` | 1.1.0 | ✅ New tool |
| `epics.delete` | 1.1.0 | ✅ New tool |
| `stories.delete` | 1.1.0 | ✅ New tool |
| `tasks.delete` | 1.1.0 | ✅ New tool |
| `append_description` | 1.1.0 | ✅ Optional param |
| `add_tags` | 1.1.0 | ✅ Optional param |
| `archive_or_close` | 1.1.0 | ✅ New tool |
| `include_details` | 1.1.0 | ✅ Optional param (default: false) |
| `page_size` default | 1.1.0 | ✅ Default added (50) |

**Upgrade Impact:** None - all changes optional and additive.

---

## Quick Links

- **Full Documentation:** [TAIGA_MCP_ENHANCEMENTS.md](TAIGA_MCP_ENHANCEMENTS.md)
- **Test Suite:** [test_enhancements.py](test_enhancements.py)
- **Summary:** [ENHANCEMENT_SUMMARY.md](ENHANCEMENT_SUMMARY.md)
- **Main Server:** [app.py](app.py)

---

**Questions?** Contact ai-engineering@offset3.com
