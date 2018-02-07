# How to use

1. virtualenv-3.6 venv3
1. venv3/bin/activate
1. pip install -r requirements.txt
1. python tigris2github.py -h
1. run again with appropriate command line flags


# Command line options
```
usage: tigris2github.py [-h] --username USERNAME --password PASSWORD --repo
                        REPO --attachment_repo ATTACHMENT_REPO [--skip_import]
                        [--skip_upload_to_github] [--sanity_check]
                        [--start_issue START_ISSUE] [--end_issue END_ISSUE]
                        [--relationship_only]

Migrate bugs from tigris bug tracke to Github issues

optional arguments:
  -h, --help            show this help message and exit
  --username USERNAME   GitHub username
  --password PASSWORD   GitHub password or Personal access token if 2FA is
                        enabled
  --repo REPO           Target GitHub Repo for issues form is SCons/SCons (not
                        https...)
  --attachment_repo ATTACHMENT_REPO
                        GitHub Repo to copy tigris bug attachements to
  --skip_import         Skip importing from tigris, use existing local cache
  --skip_upload_to_github
                        Upload the tigris bugs to github
  --sanity_check        Run sanity checks on mapping
  --start_issue START_ISSUE
                        Start at this tigris issue
  --end_issue END_ISSUE
                        End at his tigris issue
  --relationship_only   Only update the relationships
```

# tigris-to-github
Tool to migrate tigris bugs to github.

Initial code is Dirk's work to migrate tigris->roundup.

Other similar work:

https://github.com/JamesMGreene/gc2gh-issue-migrator

https://groups.google.com/forum/#!topic/silverstripe-dev/MXBJoYUHTkg

https://stackoverflow.com/questions/7281304/migrate-bugzilla-issues-to-github-issue-tracker

http://numpy-discussion.10968.n7.nabble.com/Migrating-issues-to-GitHub-td31124.html
