window.GIT_STATUS = {
  "project": "ha-tools-hub",
  "repo_url": "https://github.com/doobee24/ha-tools-hub",
  "run_at": "2026-03-25T18:01:01.272Z",
  "overall": "fail",
  "summary": {
    "passed": 0,
    "warned": 0,
    "failed": 5,
    "total": 5
  },
  "checks": [
    {
      "id": 1,
      "name": "Git Initialised & Branch Clean",
      "command": "git status",
      "status": "fail",
      "detail": "Git may not be initialised or branch unexpected. Output: 'git' is not recognized as an internal or external command,\r\noperable program or batch file."
    },
    {
      "id": 2,
      "name": "Remote Origin Connected",
      "command": "git remote -v",
      "status": "fail",
      "detail": "No remote origin found. Run: git remote add origin https://github.com/doobee24/ha-tools-hub.git"
    },
    {
      "id": 3,
      "name": "Commits Exist",
      "command": "git log --oneline",
      "status": "fail",
      "detail": "No commits found. Run: git add . && git commit -m 'Initial commit'"
    },
    {
      "id": 4,
      "name": "Files Pushed to GitHub",
      "command": "git ls-remote origin",
      "status": "fail",
      "detail": "Could not reach GitHub. Check internet/PAT credentials. 'git' is not recognized as an internal or external command,\r\noperable program or batch file."
    },
    {
      "id": 5,
      "name": "Branch Tracking Remote",
      "command": "git branch -vv",
      "status": "fail",
      "detail": "No branches found. Ensure git is initialised and a commit has been made."
    }
  ]
};