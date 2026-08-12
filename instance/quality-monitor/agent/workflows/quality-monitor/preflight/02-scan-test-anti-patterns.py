#!/usr/bin/env python3
"""Scan repos for test anti-patterns (KEDA scheduled)."""

import subprocess
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from common import (
    load_project_repos,
    output_result,
    get_capacity,
    get_tasks,
)
from lib.config import load_config

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Timeout and limit constants
TIMEOUT_API_LIST = 10  # GitHub API tree listing
MAX_TEST_FILES_PER_REPO = 20  # Max test files to analyze per repo
MAX_CONCURRENT_SCANS = 5  # Max test scan tasks to process at once
DEFAULT_MAX_REPOS_PER_SCAN = 3  # Default repos to scan per run


def detect_framework(repo_path: Path, config: Optional[Dict]) -> Optional[str]:
    """Auto-detect test framework based on indicators.

    Args:
        repo_path: Path to repository
        config: Test configuration dict

    Returns:
        Framework name or None if not detected
    """
    if not config or "defaults" not in config:
        return None

    framework_detection = config["defaults"].get("framework_detection", {})

    for framework, detection in framework_detection.items():
        indicators = detection.get("indicators", [])
        for indicator in indicators:
            # Check for exact file or glob pattern
            if "*" in indicator:
                if list(repo_path.glob(indicator)):
                    logger.debug(f"Detected {framework} via glob pattern {indicator}")
                    return framework
            else:
                if (repo_path / indicator).exists():
                    logger.debug(f"Detected {framework} via file {indicator}")
                    return framework

    return None


def get_test_patterns(
    repo_name: str, repo_path: Path, config: Optional[Dict]
) -> Tuple[List[str], List[str]]:
    """Get test file patterns for a repo (custom or auto-detected).

    Args:
        repo_name: Repository name
        repo_path: Path to repository
        config: Test configuration dict

    Returns:
        Tuple of (patterns, excludes) lists
    """
    if not config:
        # Fallback to hardcoded patterns
        logger.debug(f"{repo_name}: Using fallback patterns (no config)")
        return [
            "**/*.test.js",
            "**/*.test.ts",
            "**/*.test.jsx",
            "**/*.test.tsx",
            "**/*.spec.js",
            "**/*.spec.ts",
            "**/*.spec.jsx",
            "**/*.spec.tsx",
            "**/test_*.py",
            "**/*_test.py",
            "**/*Test.java",
        ], []

    # Check for repo-specific config
    repos_config = config.get("repos") if config else None
    if repos_config and repo_name in repos_config:
        repo_cfg = repos_config[repo_name]
        logger.info(f"{repo_name}: Using repo-specific config")
        return repo_cfg.get("patterns", []), repo_cfg.get("exclude", [])

    # Auto-detect framework
    framework = detect_framework(repo_path, config)

    defaults = config.get("defaults", {})
    global_excludes = defaults.get("global_excludes", [])

    if framework:
        framework_config = defaults.get("framework_detection", {}).get(framework, {})
        patterns = framework_config.get("patterns", [])
        logger.info(f"{repo_name}: Detected {framework}, using framework patterns")
        return patterns, global_excludes

    # Use generic fallback
    generic_patterns = defaults.get("generic_patterns", [])
    logger.info(f"{repo_name}: No framework detected, using generic patterns")
    return generic_patterns, global_excludes


def expand_brace_patterns(pattern: str) -> List[str]:
    """Expand {a,b,c} patterns into multiple patterns.

    Args:
        pattern: Glob pattern potentially containing braces

    Returns:
        List of expanded patterns
    """
    match = re.search(r"\{([^}]+)\}", pattern)
    if not match:
        return [pattern]

    options = match.group(1).split(",")
    results = []
    for option in options:
        expanded = pattern[: match.start()] + option + pattern[match.end() :]
        # Recursively expand if more braces exist
        results.extend(expand_brace_patterns(expanded))
    return results


def find_test_files_from_list(
    file_list: List[str],
    repo_name: str,
    config: Optional[Dict],
    max_files: int = MAX_TEST_FILES_PER_REPO,
) -> List[Dict[str, Any]]:
    """Find test files from file list using config patterns.

    Args:
        file_list: List of file paths from repository
        repo_name: Repository name
        config: Test configuration dict
        max_files: Maximum files to return

    Returns:
        List of dicts with path
    """
    test_files = []

    # Safety check
    if not file_list:
        return test_files

    # Get patterns - use generic since we don't have local repo to detect framework
    if (
        config
        and "repos" in config
        and config.get("repos")
        and repo_name in config["repos"]
    ):
        patterns = config["repos"][repo_name].get("patterns", [])
        excludes = config["repos"][repo_name].get("exclude", [])
    elif config and "defaults" in config:
        # Try framework detection from indicators
        framework = None
        for fw, detection in config["defaults"].get("framework_detection", {}).items():
            for indicator in detection.get("indicators", []):
                if any(indicator in f for f in file_list if f):
                    framework = fw
                    break
            if framework:
                break

        if framework:
            patterns = (
                config["defaults"]
                .get("framework_detection", {})
                .get(framework, {})
                .get("patterns", [])
            )
        else:
            patterns = config["defaults"].get("generic_patterns", [])
        excludes = config["defaults"].get("global_excludes", [])
    else:
        # Fallback patterns
        patterns = [
            "**/*.spec.ts",
            "**/*.test.ts",
            "**/*.spec.js",
            "**/*.test.js",
        ]
        excludes = ["**/node_modules/**", "**/dist/**"]

    # Expand brace patterns
    expanded_patterns = []
    for pattern in patterns:
        expanded_patterns.extend(expand_brace_patterns(pattern))

    logger.debug(f"{repo_name}: Matching with {len(expanded_patterns)} patterns")

    # Match files against patterns
    for file_path in file_list:
        if len(test_files) >= max_files:
            break

        # Check excludes first
        excluded = False
        for exclude_pattern in excludes:
            # Convert glob to simple string matching
            exclude_simple = exclude_pattern.replace("**/", "").replace("/**", "")
            if exclude_simple in file_path:
                excluded = True
                break

        if excluded:
            continue

        # Check if file matches any pattern
        for pattern in expanded_patterns:
            # Simple glob matching for common patterns
            pattern_simple = pattern.replace("**/", "")
            if pattern_simple.endswith("*.spec.ts") and file_path.endswith(".spec.ts"):
                test_files.append({"path": file_path})
                break
            elif pattern_simple.endswith("*.test.ts") and file_path.endswith(
                ".test.ts"
            ):
                test_files.append({"path": file_path})
                break
            elif pattern_simple.endswith("*.spec.js") and file_path.endswith(
                ".spec.js"
            ):
                test_files.append({"path": file_path})
                break
            elif pattern_simple.endswith("*.test.js") and file_path.endswith(
                ".test.js"
            ):
                test_files.append({"path": file_path})
                break

    logger.info(f"{repo_name}: Found {len(test_files)} test files")
    return test_files


def list_repo_files_via_api(org_repo: str, branch: str = "main") -> Optional[List[str]]:
    """List repository files via GitHub API without cloning.

    Args:
        org_repo: Repository in 'owner/repo' format
        branch: Branch name (default: main)

    Returns:
        List of file paths or None if error
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{org_repo}/git/trees/{branch}?recursive=1",
                "--jq",
                '.tree[] | select(.type == "blob") | .path',
            ],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_API_LIST,
        )

        if result.returncode != 0:
            # Try master if main fails
            if branch == "main":
                logger.debug(f"{org_repo}: main branch failed, trying master")
                return list_repo_files_via_api(org_repo, "master")
            logger.warning(
                f"gh api failed for {org_repo} (branch: {branch}): {result.stderr.strip()}"
            )
            return None

        files = [line.strip() for line in result.stdout.strip().split("\n") if line]
        logger.info(f"{org_repo}: Listed {len(files)} files via API")
        return files

    except subprocess.TimeoutExpired:
        logger.warning(
            f"Timeout listing files for {org_repo} after {TIMEOUT_API_LIST}s"
        )
        return None


def main():
    """Main entry point for test anti-pattern scanner."""
    # Check capacity
    active_n, max_n = get_capacity()
    if active_n >= max_n:
        logger.info(f"At capacity ({active_n}/{max_n})")
        output_result("skip", f"At capacity ({active_n}/{max_n})")
        return

    # Check for existing test scan tasks
    tasks = get_tasks()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_scans = [
        t
        for t in tasks
        if t.get("external_key", "").startswith(f"test-scan:")
        and today in t.get("external_key", "")
    ]
    if today_scans:
        logger.info(
            f"Already scanned today ({len(today_scans)} task(s) with date {today})"
        )
        output_result("skip", f"Already scanned today ({len(today_scans)} repos)")
        return

    active_scans = [
        t
        for t in tasks
        if t.get("external_key", "").startswith("test-scan:")
        and t.get("status") in ("in_progress", "pr_open", "pr_changes")
    ]
    if len(active_scans) >= MAX_CONCURRENT_SCANS:
        logger.info(f"Already processing {len(active_scans)} test scans")
        output_result("skip", f"Already processing {len(active_scans)} test scans")
        return

    # Load test config
    test_config = load_config()
    max_repos = (
        test_config.get("limits", {}).get(
            "max_repos_per_scan", DEFAULT_MAX_REPOS_PER_SCAN
        )
        if test_config
        else DEFAULT_MAX_REPOS_PER_SCAN
    )

    repos = load_project_repos()
    repos_with_tests: Dict[str, Dict[str, Any]] = {}

    # Filter repos if scan_only_repos is configured
    scan_only = test_config.get("scan_only_repos") if test_config else None
    if scan_only is not None:
        logger.info(f"Filtering to scan_only_repos: {scan_only}")
        repos = {k: v for k, v in repos.items() if k in scan_only}
        if not repos:
            logger.warning("No repos match scan_only_repos filter")
            output_result("skip", "No repositories configured in scan_only_repos")
            return

    # Limit repos per run
    repo_list = list(repos.keys())[:max_repos]
    logger.info(f"Scanning {len(repo_list)} repositories for test files via GitHub API")

    for repo_name in repo_list:
        # Extract org/repo from upstream URL
        upstream_url = repos[repo_name].get("upstream")
        if not upstream_url:
            logger.warning(f"{repo_name}: No upstream URL configured")
            continue

        # Parse org/repo from URL (e.g., https://github.com/RedHatInsights/insights-chrome)
        if "github.com" not in upstream_url:
            logger.warning(f"{repo_name}: Not a GitHub repository, skipping")
            continue

        org_repo = upstream_url.split("github.com/")[-1].replace(".git", "")

        # List files via GitHub API (no cloning needed)
        file_list = list_repo_files_via_api(org_repo)
        if not file_list:
            logger.warning(f"{repo_name}: Failed to list files via GitHub API")
            continue

        # Find test files from file list
        test_files = find_test_files_from_list(
            file_list, repo_name, test_config, MAX_TEST_FILES_PER_REPO
        )

        if test_files:
            # Detect framework from file list
            framework = "generic"
            if test_config and "defaults" in test_config:
                for fw, detection in (
                    test_config["defaults"].get("framework_detection", {}).items()
                ):
                    for indicator in detection.get("indicators", []):
                        if any(indicator in f for f in file_list):
                            framework = fw
                            break
                    if framework != "generic":
                        break

            repos_with_tests[repo_name] = {
                "files": test_files,
                "framework": framework,
            }

    if not repos_with_tests:
        logger.info(f"Scanned {min(max_repos, len(repos))} repos, no test files found")
        output_result(
            "skip", f"Scanned {min(max_repos, len(repos))} repos, no test files found"
        )
        return

    total_files = sum(len(data["files"]) for data in repos_with_tests.values())
    logger.info(
        f"Found {total_files} test files across {len(repos_with_tests)} repositories"
    )

    content = f"# Test Files for Anti-Pattern Analysis ({total_files} files)\n\n"

    for repo, data in repos_with_tests.items():
        test_files = data["files"]
        framework = data["framework"]

        # Compact format: comma-separated file list
        file_list = ", ".join(f["path"] for f in test_files)

        content += f"{repo} ({len(test_files)} files, {framework}):\n"
        content += f"  {file_list}\n\n"

    output_result("start", content)


if __name__ == "__main__":
    main()
