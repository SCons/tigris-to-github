#!/bin/env/python

import sys
import argparse
import base64
import getpass
import glob
import html
import json
import re
import tempfile
import time
import pprint

from github import Github, UnknownObjectException
import lxml
import lxml.etree
import requests

import import_tigris

my_printer = pp = pprint.PrettyPrinter(indent=4)

def escape_issue_markdown_repl(matchobj):
    '''Prevent issue-like text in Tigris issues from incorrectly linking issues.'''
    return '#<span></span>' + matchobj.group(1)


def get_target_milestone(tigris_issue, gh_issue):
    '''Get a GitHub milestone if the Tigris issue has one.'''
    milestone = None
    tigris_milestone = tigris_issue.xpath('target_milestone')[0].text
    if tigris_milestone != '-unspecified-':
        for m in gh_issue.repository.get_milestones():
            if m.title == tigris_milestone:
                milestone = m
                break
        if not milestone:
            milestone = gh_issue.repository.create_milestone(
                tigris_milestone, description="Created automatically")
    return milestone


def import_issue_file_loc(tigris_issue, gh_issue):
    '''optional'''
    issue_file_loc = tigris_issue.xpath('issue_file_loc')
    if issue_file_loc and issue_file_loc[0].text:
        gh_issue.edit(
            body=gh_issue.body +
            '\r\nMore information about this issue is at ' +
            issue_file_loc[0].text +
            '.\r\n')


def import_votes(tigris_issue, gh_issue):
    '''optional'''
    votes = tigris_issue.xpath('votes')
    if votes and votes[0].text:
        gh_issue.edit(body=gh_issue.body +
                      '\r\nVotes for this issue: ' + votes[0].text + '.\r\n')


def get_keyword_labels(tigris_issue):
    '''Create a label for each keyword.'''
    # There can be many keywords fields, each containing comma-separated
    # values.
    labels = []
    if len(tigris_issue.xpath('keywords')) > 0:
        for keywords in tigris_issue.xpath('keywords'):
            for keyword in keywords:
                if keyword.text:
                    labels.append(keyword.text.split(',').strip())
    return labels


def get_labels(tigris_issue):
    labels = get_keyword_labels(tigris_issue)
    for field_name, default_value in (
        ('component', 'scons'),
        ('version', '-unspecified-'),
        ('rep_platform', 'All'),
        ('subcomponent', 'scons'),
        ('op_sys', 'All')
    ):
        field_value = tigris_issue.xpath(field_name)[0].text
        if field_value and (field_value != default_value):
            labels.append(str(field_name.replace(
                '_', ' ').title() + ': ' + field_value))
    priority = tigris_issue.xpath('priority')[0].text
    if priority:
        labels.append(priority)
    # Map between Tigris issue_type and the default labels on GitHub.
    type_map = {
        'DEFECT': 'bug',
        'ENHANCEMENT': 'enhancement'
    }
    issue_type = type_map.get(tigris_issue.xpath('issue_type')[0].text)
    if issue_type:
        labels.append(issue_type)
    # Map between Tigris resolutions and the default labels on GitHub.
    resolution_map = {
        'DUPLICATE': 'duplicate',
        'INVALID': 'invalid',
        'WONTFIX': 'wontfix'
    }
    resolution_map = resolution_map.get(
        tigris_issue.xpath('resolution')[0].text)
    if resolution_map:
        labels.append(resolution_map)
    return labels


def get_relationship_text(tigris_issue, gh_issue, tigris_to_github, field_name, relationship):
    suffix = ''
    sorted_fields = sorted(tigris_issue.xpath(field_name), key=lambda x: x.xpath('when')[0].text)

    for field in sorted_fields:
        if not field.xpath('issue_id')[0].text:
            # Some relationships are empty, so skip over them.
            continue
        
        # Get the tigris issue ID this bug has a relationship with
        rel_issue_id = int(field.xpath('issue_id')[0].text)

        suffix += '\r\n' + field.xpath('who')[0].text
        suffix += ' said this issue ' + relationship + ' #'

        # Use the github issue id for the related tigris issue id
        suffix += str(tigris_to_github[rel_issue_id])
        suffix += ' at ' + field.xpath('when')[0].text + '.\r\n'
    return suffix


def add_relationships(tigris_issue, gh_issue, tigris_to_github, tigris_id, gh_id):
    '''Add the relationships between issues to GitHub.
    :Param tigris_issue: XML Element with the current issue.
    :Param gh_issue: handle to github issue
    :Param tigris_to_github: map from tigris issue number to github issue number
    :Param tigris_id: the number of the current tigris issue
    :Param gh_id: The number of the github issue
    '''
    suffix = ''
    for field_name, relationship in (
        ('dependson', 'depends on'),
        ('blocks', 'blocks'),
        ('is_duplicate', ' is a duplicate of'),
        ('has_duplicates', 'is duplicated by'),
    ):
        suffix += get_relationship_text(tigris_issue, gh_issue,
                                        tigris_to_github, field_name, relationship)
    if suffix:
        print("Adding Relationship info for Tigris issue: %d [GH %d]"%(tigris_id, gh_id))
        gh_issue.edit(body=gh_issue.body + suffix)


def add_issue_relationships(gh_id, tigris_issue, tigris_id, issue_repo, gh, tigris_to_github):
    """
    :Param gh_id: Integer - The github issue #
    :Param tigris_issue: The xml Element for the tigris issue
    :Param tigris_id: The tigris issue id
    :Param issue_repo: Handle to the github repo we're adding issues to
    :Param gh: The Github handle
    :Param tigris_to_github: Dictionary mapping tigris issue # to github issue #
    """
    print("Checking issue relationships for:%d [Github issue:%d]"%(tigris_id, gh_id))

    gh_issue = issue_repo.get_issue(gh_id)
    reset_time = gh.rate_limiting_resettime
    if gh.rate_limiting[0] < 10:
        delay = 10 + (reset_time - time.time())
        print('Waiting ' + delay + 's for rate limit to reset.')
        time.sleep(delay)
    add_relationships(tigris_issue, gh_issue, tigris_to_github, tigris_id, gh_id)
    time.sleep(1)

def import_attachment(tigris_issue, gh_issue, attachment_repo, args):
    '''PyGithub doesn't support the Contents endpoint of the GitHub REST API
    https://developer.github.com/v3/repos/contents/.
    :Param tigris_issue: lxml element with all info from tigris issue
    :Param gh_issue: 
    :Param attachment_repo: Github handle to attachment_repo
    :Param args: command line argument values
    '''
    suffix = ''

    tigris_issue_id = tigris_issue.xpath('issue_id')[0].text

    url_prefix = '/'.join(('https://api.github.com/repos',
                           args.attachment_repo, 'contents', tigris_issue_id))
    sorted_attachments = sorted(tigris_issue.xpath(
        'attachment'), key=lambda x: x.xpath('date')[0].text)



    for attachment in sorted_attachments:
        attachid = attachment.xpath('attachid')[0].text
        filename = attachment.xpath('filename')[0].text
        who = attachment.xpath('submitting_username')[0].text
        if not who:
            who = 'An anonymous user'
        url_suffix = attachid + '/' + filename
        dest_url = url_prefix + '/' + url_suffix
        comment_url = '/'.join(('https://github.com',
                                args.attachment_repo, 'blob/master', tigris_issue_id, url_suffix))
        suffix += '\r\n' + who
        suffix += ' attached [' + filename + '](' + comment_url + ')'
        suffix += ' at ' + attachment.xpath('date')[0].text + '.\r\n'
        desc = attachment.xpath('desc')[0].text
        if desc:
            suffix += '>' + desc + '\r\n'

        # Copy the attachment to a temporary file, and upload to GitHub.
        # Tigris can be flakey, so retry with a delay if the connection
        # closed by Tigris.
        src_url = attachment.xpath('attachment_iz_url')[0].text
        num_retries = 0
        while num_retries < 10:
            try:
                r = requests.get(src_url, stream=True)
                break
            except Exception as e:
                print("import_attachment(): Exception-->%s"%e)
                num_retries += 1
                time.sleep(5)
        with tempfile.TemporaryFile() as fd:
            for chunk in r.iter_content(chunk_size=128):
                fd.write(chunk)
            fd.seek(0)
            payload = {
                "path": url_suffix,
                "message": "Add issue attachment taken from " + src_url,
                "content": base64.b64encode(fd.read()).decode('ascii')
            }
            requests.put(dest_url, auth=(args.username, args.password),
                         data=json.dumps(payload))
    if suffix:
        gh_issue.edit(body=gh_issue.body + suffix)

def upload_to_github(tigris_issue, repo, mapping, attachment_repo, args):
    '''Import a single Tigris issue into a GitHub repo.

    :param tigris_issue: The source issue
    :param repo: The destination GitHub repository for issues
    :param mapping: Mapping from tigris issue id to github id to avoid overwritting existing PRs
    :param attachment_repo: The destination GitHub repository for attachments
    :param args: command line argument values
    '''

    tigris_issue_id = int(tigris_issue.xpath('issue_id')[0].text)
    issue_id = mapping[tigris_issue_id]

    title = html.unescape(tigris_issue.xpath('short_desc')[0].text)

    # Overwrite an existing issue, if present.
    try:
        gh_issue = repo.get_issue(issue_id)

        # Verify we're not going to overwrite a pull_request.
        if gh_issue.pull_request is not None:
            print("Trying to update GitHub issue %d and it's a pull request exiting"%issue_id)
            sys.exit(-1)

    except UnknownObjectException:
        # Sleep here to follow GitHub's guideline to wait a second between requests. See
        # https://developer.github.com/v3/guides/best-practices-for-integrators/#dealing-with-abuse-rate-limits
        time.sleep(1)
        gh_issue = repo.create_issue(title)

    time.sleep(5)

    print('Importing Tigris issue {} as new issue {}: "{}"'.format(tigris_issue_id, issue_id, title))
    if gh_issue.number != issue_id:
        print(issue_id, gh_issue.number)
        # Someone's created an issue whilst we working, overwrite theirs.
        gh_issue = repo.get_issue(issue_id)

    state = 'open'
    if tigris_issue.xpath('issue_status')[0].text in (
            'RESOLVED', 'CLOSED', 'VERIFIED'):
        state = 'closed'
    # Create the initial body of the issue.
    body = 'This issue was originally created at: ' + \
        tigris_issue.xpath('creation_ts')[0].text + '.\r\n'
    reporter = tigris_issue.xpath('reporter')[0].text
    if reporter:
        body += 'This issue was reported by: `' + reporter + '`.\r\n'

    sorted_long_descs = sorted(tigris_issue.xpath(
        'long_desc'), key=lambda x: x.xpath('issue_when')[0].text)

    for long_desc in sorted_long_descs:
        body += long_desc.xpath('who')[0].text
        body += ' said at '
        body += long_desc.xpath('issue_when')[0].text
        long_desc_text = long_desc.xpath('thetext')[0].text
        if not long_desc_text:
            long_desc_text = 'No text was provided with this entry.'
        unescaped_long_desc_text = html.unescape(long_desc_text)
        for line in unescaped_long_desc_text.splitlines():
            if line:
                # Edit anything of the form '#number' as this is parsed by 
                # GitHub's markdown as a link to another issue.
                line = re.sub(r'#(\d+)', escape_issue_markdown_repl, line)
            else:
                # GitHub's markdown doesn't tolerate empty quote lines.
                line = ' '
            body += '\r\n>' + line
        body += '\r\n\r\n'
    gh_issue.edit(
        title=title,
        body=body,
        state=state,
        milestone=get_target_milestone(tigris_issue, gh_issue),
        labels=get_labels(tigris_issue)
    )

    # Each function maps to a field in the Tigris issue, based on the DTD at
    # http://scons.tigris.org/issues/issuezilla.dtd
    import_issue_file_loc(tigris_issue, gh_issue)
    import_votes(tigris_issue, gh_issue)

    import_attachment(tigris_issue, gh_issue, attachment_repo, args)

def build_tigris_to_github_map(max_tigris_id, issue_repo):
    """
    It's necessary to create a mapping because GitHub shares the issue and pull
    request numbers. So if there's a pull request 1, there cannot be a issue 1.

    :param max_tigris_id: Highest numbered bug in tigris bug tracker
    :param issue_repo: handle to access the target issue repo
    :return: A dictionary mapping the tigris bug ID to the new GitHub issue
    """
    mapping = {}

    # Get the numbers of all existing pull requests
    pull_requests = issue_repo.get_pulls(state='all', direction='desc')
    pr_numbers = [p.number for p in pull_requests]
    
    # gh_issues = issue_repo.get_issues(state='all', direction='desc')
    # issue_numbers = [i.number for i in gh_issues if not i.pull_request]
    moved_issue_start_id = max(max(pr_numbers), max_tigris_id)

    current_offset = 1
    for tid in range(1, max_tigris_id+1):
        if tid in pr_numbers:
            mapping[tid] = moved_issue_start_id + current_offset
            current_offset += 1
        else:
            mapping[tid] = tid

    return (mapping, pr_numbers)

def load_all_tigris_issues():
    """
    Load all the downloaded tigrix xml files and store in a dictionary
    keyed by their tigris bug #

    :return: Dictionary with key tigris_id and contents is the xml Element
    containing all the issue info
    """
    mapping = {}
    for issue_group_file in glob.glob('xml/*.xml'):
        print("Processing: %s"%issue_group_file)
        with open(issue_group_file, 'rb') as f_in:
            issues_xml = lxml.etree.XML(f_in.read())
            
            issues = issues_xml.xpath('issue')
            for issue in issues:
                issue_id = issue.xpath('issue_id')[0].text
                mapping[int(issue_id)] = issue

    return mapping

def upload_tigris_issue_to_github(gh, issue_repo, attachment_repo, tigris_issue, mapping, args):
    """
    Upload a single issue to github.
    NOTE: This will overwrite any existing content in the github issue

    :Param gh: Main GitHub connection handle
    :Param issue_repo: GitHub handle for main repo
    :Param attachment_repo: GitHub handle for attachment repo
    :Param tigris_issue: This is a lxml xml element representing a single issue
    :Param mapping: The map of tigris issue number to github issue number
    :Param args: command line argument values
    """

    issue_id = int(tigris_issue.xpath('issue_id')[0].text)
    print("Uploading issue #%-5d"%issue_id)

    reset_time = gh.rate_limiting_resettime
    if gh.rate_limiting[0] < 10:
        delay = 10 + (reset_time - time.time())
        print(
            'Waiting ' +
            str(delay) +
            's for rate limit to reset.')
        time.sleep(delay)

    # Even though we're respecting the rate limit, and GitHub says at
    # https://developer.github.com/v3/#abuse-rate-limits that this is sufficient,
    # we still sometimes hit Abuse Rate Limit. If this happens, wait with
    # increasing delay and retry.
    num_retries = 0
    while num_retries < 10:
        try:
            upload_to_github(tigris_issue, issue_repo, mapping, attachment_repo, args)
            break
        except UnknownObjectException as e:
            print("In upload_tigris_issue_to_github(): Got Exception:%s"%e)
            print("Try another time: Skipping")
            break

        except Exception as e:
            print("In upload_tigris_issue_to_github(): Got Exception:%s"%e)
            num_retries += 1
            time.sleep(60 * num_retries)
    # Ensure that there's a second delay between successive API
    # calls.
    time.sleep(5)

def sanity_check_mapping(mapping, max_tigris_id, pr_numbers):
    """
    Do some basic checks and dump info on mapping
    :param mapping: dictionary mapping tigris id to github issue
    :param max_tigris_id: max issue # on tigris
    """
    mismatches = [(a, b) for (a, b) in mapping.items() if a != b]
    pr_less_than_max = [p for p in pr_numbers if p < max_tigris_id]

    if len(mismatches) != len(pr_less_than_max):
        print("Issue %d mismatches, %d Pull requests"%(len(mismatches), len(pr_numbers)))
    
    # Dump mapping
    my_printer.pprint(mismatches)


def process_command_line():
    parser = argparse.ArgumentParser(description="Migrate bugs from tigris bug tracke to Github issues")
    parser.add_argument('--username', required=True, help="GitHub username")
    parser.add_argument('--password', required=True, help="GitHub password or Personal access token if 2FA is enabled")
    parser.add_argument('--repo', required=True, help='Target GitHub Repo for issues form is SCons/SCons (not https...)')
    parser.add_argument('--attachment_repo', required=True, help='GitHub Repo to copy tigris bug attachements to')
    parser.add_argument('--skip_import', action='store_true', default=False, help="Skip importing from tigris, use existing local cache")
    parser.add_argument('--skip_upload_to_github', action='store_false', dest='upload_to_github', 
                        default=True, help="Upload the tigris bugs to github")
    parser.add_argument('--sanity_check', action='store_true', default=False, help='Run sanity checks on mapping')
    parser.add_argument('--start_issue', type=int, help="Start at this tigris issue")
    parser.add_argument('--end_issue', type=int, help='End at his tigris issue')
    parser.add_argument('--relationship_only', default=False, action='store_true', help='Only update the relationships')
    args = parser.parse_args()


    return args


def main():

    max_tigris_id =0
    args = process_command_line()

    if not args.skip_import:
        # Export the issues from Tigris as XML into a directory.
        max_tigris_id = import_tigris.fetch_files('scons', 'xml')

    tigris_issues = load_all_tigris_issues()
    max_issue_from_files = max(tigris_issues.keys())
    max_tigris_id = max(max_tigris_id, max_issue_from_files)

    gh = Github(args.username, args.password)

    attachment_repo = gh.get_repo(args.attachment_repo)
    issue_repo = gh.get_repo(args.repo)
    if not issue_repo.has_issues:
        print("The repo: %s doesn't have issues enabled. Please enable them and rerun")
        sys.exit(-1)

    (tigris_to_github, pr_numbers) = build_tigris_to_github_map(max_tigris_id, issue_repo)

    if args.sanity_check:
        sanity_check_mapping(tigris_to_github, max_tigris_id, pr_numbers)

    if args.upload_to_github:
        github_to_tigris = {v: k for k, v in tigris_to_github.items()}

        if not args.relationship_only:
            processed = 1
            for gh_index in sorted(github_to_tigris):
                tigris_index = github_to_tigris[gh_index]
                if args.start_issue and tigris_index < args.start_issue:
                    continue
                elif args.end_issue and tigris_index > args.end_issue:
                    continue

                if processed % 100 == 0:
                    print("upload_tigris_issue_to_github()->Sleeping every 100 for 1 minute")
                    time.sleep(60)

                upload_tigris_issue_to_github(gh, issue_repo, attachment_repo, tigris_issues[tigris_index], tigris_to_github, args)
                processed += 1


        # Now all the issues are in imported add the relationships between them.
        for tigris_id in tigris_issues:

            if args.start_issue and tigris_id < args.start_issue:
                continue
            elif args.end_issue and tigris_id > args.end_issue:
                continue

            if tigris_id % 100 == 0:
                print("add_issue_relationships()->Sleeping every 100 for 1 minute")
                time.sleep(60)


            gh_id=tigris_to_github[tigris_id]
            add_issue_relationships(gh_id, tigris_issues[tigris_id], tigris_id, issue_repo, gh, tigris_to_github)



if __name__ == '__main__':
    main()
