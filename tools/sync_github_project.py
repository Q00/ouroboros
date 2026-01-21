#!/usr/bin/env python3
"""
GitHub Project Status Sync Tool

GitHub Project V2의 Status 필드를 동기화합니다.
Story 상태가 변경될 때 GitHub Project의 상태도 함께 업데이트합니다.

Usage:
    python tools/sync_github_project.py <issue_number> <status>

Examples:
    python tools/sync_github_project.py 22 in_progress
    python tools/sync_github_project.py 22 done
"""

import os
import sys
import json
import subprocess
import urllib.request
import urllib.error
from typing import Optional, Dict


def get_github_token() -> str:
    """Get GitHub token from environment or gh CLI"""
    # Try environment variable first
    token = os.getenv("GITHUB_TOKEN")
    if token:
        return token

    # Try to get token from gh CLI
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


# Configuration from GitHub Project analysis
GITHUB_TOKEN = get_github_token()
REPO = "Q00/ouroboros"
PROJECT_ID = "PVT_kwHOAd0NXs4BMkGj"
STATUS_FIELD_ID = "PVTSSF_lAHOAd0NXs4BMkGjzg7z4Ic"

# Status option mappings (name -> option_id)
STATUS_OPTIONS = {
    "todo": "f75ad846",
    "not_started": "f75ad846",
    "in_progress": "47fc9ee4",
    "inprogress": "47fc9ee4",
    "done": "98236657",
    "completed": "98236657",
}

# Normalize status names
STATUS_NORMALIZATION = {
    "todo": "Todo",
    "not_started": "Todo",
    "in_progress": "In Progress",
    "inprogress": "In Progress",
    "done": "Done",
    "completed": "Done",
}

GRAPHQL_API = "https://api.github.com/graphql"


class GitHubProjectSync:
    """GitHub Project V2 Status Sync Tool"""

    def __init__(self, repo: str = REPO, project_id: str = PROJECT_ID):
        self.repo = repo
        self.project_id = project_id
        self.headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        }
        self._cache: Dict[str, str] = {}

    def _query_graphql(self, query: str, variables: Dict) -> Dict:
        """Execute GraphQL query"""
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")

        req = urllib.request.Request(
            GRAPHQL_API,
            data=payload,
            headers=self.headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode("utf-8"))

            if "errors" in data:
                raise RuntimeError(f"GraphQL Error: {data['errors']}")
            return data
        except urllib.error.HTTPError as e:
            error_body = json.loads(e.read().decode("utf-8"))
            raise RuntimeError(f"HTTP Error {e.code}: {error_body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"URL Error: {e.reason}")

    def get_project_item_id(self, issue_number: int) -> Optional[str]:
        """
        Get Project Item ID from Issue Number

        Args:
            issue_number: Story issue number (e.g., 22)

        Returns:
            Project Item ID or None if not found
        """
        # Check cache first
        cache_key = f"{self.repo}#{issue_number}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        query = """
        query($repo_owner: String!, $repo_name: String!, $issue_num: Int!) {
          repository(owner: $repo_owner, name: $repo_name) {
            issue(number: $issue_num) {
              projectItems(first: 10) {
                nodes {
                  id
                  project {
                    id
                  }
                }
              }
            }
          }
        }
        """

        owner, repo_name = self.repo.split("/")
        variables = {
            "repo_owner": owner,
            "repo_name": repo_name,
            "issue_num": issue_number,
        }

        data = self._query_graphql(query, variables)
        items = data["data"]["repository"]["issue"]["projectItems"]["nodes"]

        # Find item in our project
        for item in items:
            if item["project"]["id"] == self.project_id:
                self._cache[cache_key] = item["id"]
                return item["id"]

        return None

    def update_status(self, issue_number: int, status: str) -> Dict:
        """
        Update GitHub Project Status for a Story

        Args:
            issue_number: Story issue number (e.g., 22)
            status: New status ("todo", "in_progress", "done")

        Returns:
            Result dict with success and message
        """
        # Normalize status
        status_lower = status.lower().replace("-", "_").replace(" ", "")
        normalized_status = STATUS_NORMALIZATION.get(status_lower)

        if normalized_status is None:
            return {
                "success": False,
                "message": f"Invalid status: {status}. Valid options: todo, in_progress, done",
            }

        status_option_id = STATUS_OPTIONS[status_lower]

        # Get Project Item ID
        item_id = self.get_project_item_id(issue_number)
        if item_id is None:
            return {
                "success": False,
                "message": f"Issue #{issue_number} not found in project",
            }

        # Update status using GraphQL mutation
        mutation = """
        mutation($input: UpdateProjectV2ItemFieldValueInput!) {
          updateProjectV2ItemFieldValue(input: $input) {
            projectV2Item {
              id
            }
          }
        }
        """

        variables = {
            "input": {
                "projectId": self.project_id,
                "itemId": item_id,
                "fieldId": STATUS_FIELD_ID,
                "value": {
                    "singleSelectOptionId": status_option_id
                }
            }
        }

        try:
            self._query_graphql(mutation, variables)
            return {
                "success": True,
                "message": f"Updated issue #{issue_number} status to '{normalized_status}'",
                "issue_number": issue_number,
                "old_status": status,
                "new_status": normalized_status,
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Failed to update: {str(e)}",
            }


def main():
    """CLI entry point"""
    if len(sys.argv) != 3:
        print("Usage: python sync_github_project.py <issue_number> <status>")
        print("\nValid statuses: todo, in_progress, done")
        sys.exit(1)

    try:
        issue_number = int(sys.argv[1])
        status = sys.argv[2]
    except ValueError:
        print("Error: issue_number must be an integer")
        sys.exit(1)

    if not GITHUB_TOKEN:
        print("Error: GITHUB_TOKEN environment variable not set")
        sys.exit(1)

    sync = GitHubProjectSync()
    result = sync.update_status(issue_number, status)

    if result["success"]:
        print(f"✅ {result['message']}")
        sys.exit(0)
    else:
        print(f"❌ {result['message']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
