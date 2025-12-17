#!/usr/bin/env python3
"""Create issue for Taiga MCP problems."""

import asyncio
import sys
sys.path.insert(0, '.')

from taiga_client import TaigaClient

PROJECT_ID = 1746402

async def main():
    client = TaigaClient()
    await client.authenticate()
    
    try:
        # Search for Taiga MCP story
        stories = await client.list_user_stories(project_id=PROJECT_ID, q='Taiga MCP')
        
        if not stories:
            print("No Taiga MCP story found, searching for Builder Agent epic stories...")
            stories = await client.list_user_stories(project_id=PROJECT_ID, tags=['taiga'])
        
        print(f"\nFound {len(stories)} potential stories:\n")
        for s in stories[:10]:
            print(f"  #{s['ref']}: {s['subject']}")
        
        if stories:
            # Create issue against the most relevant story
            target_story = stories[0]
            
            # Get valid issue statuses for the project
            issue_statuses = await client._request("GET", "/issue-statuses", params={"project": PROJECT_ID})
            default_status = issue_statuses[0]['id'] if issue_statuses else None
            
            print(f"Using status ID: {default_status}")
            
            issue_payload = {
                'project': PROJECT_ID,
                'subject': 'Taiga MCP Permission Issues: Epic Linking and Delete Operations',
                'description': '''## Problem
The Taiga MCP service account (`AresNet_service`) lacks permissions for critical operations:

1. **Epic Linking** - Cannot link user stories to epics
   - API: `POST /epics/{epic_id}/related_userstories`
   - Error: `403 Forbidden - You do not have permission to perform this action`
   - Impact: All Sprint 15 stories (#355-358) not linked to Epic #186

2. **Epic Read** - Cannot read epic details
   - API: `GET /epics/{epic_id}`
   - Error: `403 Forbidden - You do not have permission to perform this action`
   - Impact: Cannot verify epic linkage or validate epic existence

3. **Story Delete** - Cannot delete duplicate stories
   - API: `DELETE /userstories/{story_id}`
   - Error: (Assumed same permission issue)
   - Impact: Cannot clean up duplicate Story #357

## Root Cause
Service account `AresNet_service` does not have epic management permissions in Taiga project settings.

## Required Permissions
Grant the following to `AresNet_service`:
- `epic.view` - Read epic details
- `epic.modify` - Link stories to epics
- `userstory.delete` - Remove duplicate/incorrect stories

## Workaround
Manual fixes via Taiga web UI until permissions granted.

## Related Stories
- Sprint 15 Stories: #355, #356, #357, #358 (all need epic linking)
- Epic #186 - Builder Agent (Autonomous Scrum Master)

## Discovery
Identified during automated Sprint 15 story cleanup on 2025-12-09.
''',
                'severity': 3,  # Normal
                'priority': 3,  # Normal
                'type': 1,      # Bug
                'status': default_status,
            }
            
            print(f"\nCreating issue against Story #{target_story['ref']}...")
            issue = await client.create_issue(issue_payload)
            print(f"✅ Issue created: #{issue.get('ref')} - {issue.get('subject')}")
            print(f"   URL: https://tree.taiga.io/project/johnwblack-aresnet/issue/{issue.get('ref')}")
        else:
            print("No suitable story found to attach issue to.")
    
    finally:
        await client.close()

if __name__ == '__main__':
    asyncio.run(main())
