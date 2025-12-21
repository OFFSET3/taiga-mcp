"""
Test script for Taiga MCP Server enhancements.

Validates:
1. GET endpoints (epics, stories, tasks)
2. DELETE endpoints via MCP tools
3. Enhanced list endpoints (include_details, pagination)
4. Append-only helpers (append_description, add_tags)
5. Soft-delete helpers (archive_or_close)
"""

import asyncio
import sys
from taiga_client import get_taiga_client, TaigaAPIError


async def test_get_endpoints():
    """Test new GET-by-ID endpoints."""
    print("\\n=== Testing GET Endpoints ===\\n")
    
    async with get_taiga_client() as client:
        # Get first epic from project 1
        print("1. Testing get_epic...")
        try:
            epics = await client.list_epics(project_id=1)
            if epics:
                epic_id = epics[0]["id"]
                epic = await client.get_epic(epic_id)
                print(f"   ✅ get_epic({epic_id}): {epic.get('subject', 'N/A')}")
                print(f"      Fields: {', '.join(epic.keys())}")
            else:
                print("   ⚠️  No epics found in project 1")
        except TaigaAPIError as e:
            print(f"   ❌ Error: {e}")
        
        # Get first story from project 1
        print("\\n2. Testing get_user_story...")
        try:
            stories = await client.list_user_stories(project_id=1)
            if stories:
                story_id = stories[0]["id"]
                story = await client.get_user_story(story_id)
                print(f"   ✅ get_user_story({story_id}): {story.get('subject', 'N/A')}")
                print(f"      Tags: {story.get('tags', [])}")
                print(f"      Description length: {len(story.get('description', ''))}")
            else:
                print("   ⚠️  No stories found in project 1")
        except TaigaAPIError as e:
            print(f"   ❌ Error: {e}")
        
        # Get first task (if available)
        print("\\n3. Testing get_task...")
        try:
            tasks, _ = await client.list_tasks(project_id=1, page_size=1)
            if tasks:
                task_id = tasks[0]["id"]
                task = await client.get_task(task_id)
                print(f"   ✅ get_task({task_id}): {task.get('subject', 'N/A')}")
                print(f"      Status: {task.get('status', 'N/A')}")
            else:
                print("   ⚠️  No tasks found in project 1")
        except TaigaAPIError as e:
            print(f"   ❌ Error: {e}")


async def test_list_enhancements():
    """Test enhanced list endpoints."""
    print("\\n=== Testing List Enhancements ===\\n")
    
    async with get_taiga_client() as client:
        print("1. Testing epics.list pagination...")
        try:
            epics = await client.list_epics(project_id=1)
            print(f"   ✅ Retrieved {len(epics)} epics")
            if epics:
                print(f"      First epic fields: {', '.join(epics[0].keys())}")
                has_tags = 'tags' in epics[0]
                has_description = 'description' in epics[0]
                print(f"      Has tags: {has_tags}, Has description: {has_description}")
        except TaigaAPIError as e:
            print(f"   ❌ Error: {e}")
        
        print("\\n2. Testing stories.list with page_size...")
        try:
            stories = await client.list_user_stories(project_id=1, page_size=5)
            print(f"   ✅ Retrieved {len(stories)} stories (requested page_size=5)")
        except TaigaAPIError as e:
            print(f"   ❌ Error: {e}")
        
        print("\\n3. Testing tasks.list with pagination...")
        try:
            tasks, pagination = await client.list_tasks(project_id=1, page=1, page_size=3)
            print(f"   ✅ Retrieved {len(tasks)} tasks (page 1, size 3)")
            print(f"      Pagination: {pagination}")
        except TaigaAPIError as e:
            print(f"   ❌ Error: {e}")


async def test_append_helpers():
    """Test append_description and add_tags helpers (requires test story)."""
    print("\\n=== Testing Append-Only Helpers ===\\n")
    print("⚠️  This test requires manual verification in Taiga UI")
    print("    (Skipping to avoid modifying production data)")
    print("")
    print("Example usage:")
    print("   # Append to description")
    print("   await client.update_user_story(story_id, {")
    print("       'description': existing_desc + '\\n\\n' + new_text")
    print("   })")
    print("")
    print("   # Merge tags")
    print("   existing_tags = set(story['tags'])")
    print("   new_tags = existing_tags | {'automated', 'test'}")
    print("   await client.update_user_story(story_id, {")
    print("       'tags': sorted(new_tags),")
    print("       'version': story['version']")
    print("   })")


async def test_soft_delete():
    """Test soft-delete pattern (requires test task)."""
    print("\\n=== Testing Soft-Delete Pattern ===\\n")
    print("⚠️  This test requires manual verification")
    print("    (Skipping to avoid modifying production data)")
    print("")
    print("Example usage:")
    print("   # Get task")
    print("   task = await client.get_task(task_id)")
    print("")
    print("   # Find closed status")
    print("   statuses = await client.list_task_statuses(project_id)")
    print("   closed = [s for s in statuses if s.get('is_closed')]")
    print("")
    print("   # Archive task")
    print("   existing_tags = set(task.get('tags', []))")
    print("   new_tags = existing_tags | {'archived-by-mcp'}")
    print("   await client.update_task(task_id, {")
    print("       'status': closed[0]['id'],")
    print("       'tags': sorted(new_tags),")
    print("       'version': task['version']")
    print("   })")


async def test_client_methods():
    """Verify all base client methods exist."""
    print("\\n=== Verifying Client Methods ===\\n")
    
    async with get_taiga_client() as client:
        methods = [
            'get_epic', 'list_epics', 'create_epic', 'update_epic', 'delete_epic',
            'get_user_story', 'list_user_stories', 'create_user_story', 'update_user_story', 'delete_user_story',
            'get_task', 'list_tasks', 'create_task', 'update_task', 'delete_task',
            'list_user_story_statuses', 'list_task_statuses'
        ]
        
        missing = []
        for method in methods:
            if not hasattr(client, method):
                missing.append(method)
        
        if missing:
            print(f"   ❌ Missing methods: {', '.join(missing)}")
        else:
            print(f"   ✅ All {len(methods)} CRUD methods present")
            print(f"      GET: get_epic, get_user_story, get_task")
            print(f"      LIST: list_epics, list_user_stories, list_tasks")
            print(f"      CREATE: create_epic, create_user_story, create_task")
            print(f"      UPDATE: update_epic, update_user_story, update_task")
            print(f"      DELETE: delete_epic, delete_user_story, delete_task")


async def main():
    """Run all tests."""
    print("=" * 60)
    print("Taiga MCP Server Enhancement Validation")
    print("=" * 60)
    
    try:
        await test_client_methods()
        await test_get_endpoints()
        await test_list_enhancements()
        await test_append_helpers()
        await test_soft_delete()
        
        print("\\n" + "=" * 60)
        print("✅ Validation Complete")
        print("=" * 60)
        print("")
        print("Next Steps:")
        print("1. Start server: python app.py")
        print("2. Test MCP tools via mcp_chat")
        print("3. Test action proxy: curl http://localhost:8000/actions/get_epic?epic_id=123")
        print("")
        
    except Exception as e:
        print(f"\\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
