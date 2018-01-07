#!/bin/env/python

import base64
import getpass
import glob
import html
import json
import tempfile
import time

from github import Github, UnknownObjectException
import lxml
import lxml.etree
import requests

import import_tigris


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


def get_relationship_text(tigris_issue, gh_issue, field_name, relationship):
    suffix = ''
    sorted_fields = sorted(tigris_issue.xpath(
        field_name), key=lambda x: x.xpath('when')[0].text)
    for field in sorted_fields:
        suffix += '\r\n' + field.xpath('who')[0].text
        suffix += ' said this issue ' + relationship + ' #'
        suffix += field.xpath('issue_id')[0].text
        suffix += ' at ' + field.xpath('when')[0].text + '.\r\n'
    return suffix


def add_relationships(tigris_issue, gh_issue):
    '''Add the relationships between issues to GitHub.'''
    suffix = ''
    for field_name, relationship in (
        ('dependson', 'depends on'),
        ('blocks', 'blocks'),
        ('is_duplicate', ' is a duplicate of'),
        ('has_duplicates', 'is duplicated by'),
    ):
        suffix += get_relationship_text(tigris_issue,
                                        gh_issue, field_name, relationship)
    if suffix:
        gh_issue.edit(body=gh_issue.body + suffix)


def import_attachment(tigris_issue, gh_issue, user, passwd, attachment_repo):
    '''PyGithub doesn't support the Contents endpoint of the GitHub REST API
    https://developer.github.com/v3/repos/contents/.
    '''
    suffix = ''
    url_prefix = '/'.join(('https://api.github.com/repos',
                           attachment_repo, 'contents'))
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
                                attachment_repo, 'blob/master', url_suffix))
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
                print(e)
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
            requests.put(dest_url, auth=(user, passwd),
                         data=json.dumps(payload))
    if suffix:
        gh_issue.edit(body=gh_issue.body + suffix)


def import_to_github(tigris_issue, repo, user, passwd, attachment_repo):
    '''Import a single Tigris issue into a GitHub repo.

    :param tigris_issue: The source issue
    :param repo: The destination GitHub repository for issues
    :param user: GitHub username
    :param passwd: GitHub password
    :param attachment_repo: The destination GitHub repository for attachments
    '''
    issue_id = int(tigris_issue.xpath('issue_id')[0].text)
    title = html.unescape(tigris_issue.xpath('short_desc')[0].text)
    # Overwrite an existing issue, if present.
    try:
        gh_issue = repo.get_issue(issue_id)
    except UnknownObjectException:
        # Sleep here to follow GitHub's guideline to wait a second between requests. See
        # https://developer.github.com/v3/guides/best-practices-for-integrators/#dealing-with-abuse-rate-limits
        time.sleep(1)
        gh_issue = repo.create_issue(title)
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
            if not line:
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

    import_attachment(tigris_issue, gh_issue, user, passwd, attachment_repo)


def main():
    # Export the issues from Tigris as XML into a directory.
    import_tigris.fetch_files('scons', 'xml')

    # Import the issues into a GitHub repository.
    # Store any Tigris issue attachments in a (probably different) repository.
    user = input("GitHub username: ")
    passwd = getpass.getpass(prompt="GitHub Password: ")
    gh = Github(user, passwd)
    # Assume that the 'organization' associated with the repos is the user.
    # TODO relax this constraint.
    issue_repo = gh.get_repo(input("GitHub repository for issues: "))
    attachment_repo = input("GitHub repository for attachments: ")
    assert issue_repo.has_issues
    for issue_group_file in glob.glob('xml/*.xml'):
        with open(issue_group_file, 'rb') as f_in:
            issues_xml = lxml.etree.XML(f_in.read())
            sorted_issues = sorted(issues_xml.xpath('issue'),
                                   key=lambda x: int(
                                       x.xpath('issue_id')[0].text)
                                   )
            for tigris_issue in sorted_issues:
                issue_id = int(tigris_issue.xpath('issue_id')[0].text)
                print(issue_id)
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
                        import_to_github(tigris_issue, issue_repo,
                                         user, passwd, attachment_repo)
                        break
                    except Exception as e:
                        print(e)
                        num_retries += 1
                        time.sleep(60 * num_retries)
                # Ensure that there's a second delay between successive API
                # calls.
                time.sleep(1)
    # Now all the issues are in imported add the relationships between them.
    for issue_group_file in glob.glob('xml/*.xml'):
        with open(issue_group_file, 'rb') as f_in:
            issues_xml = lxml.etree.XML(f_in.read())
            for tigris_issue in issues_xml:
                issue_id = int(tigris_issue.xpath('issue_id')[0].text)
                print(issue_id)
                gh_issue = issue_repo.get_issue(issue_id)
                reset_time = gh.rate_limiting_resettime
                if gh.rate_limiting[0] < 10:
                    delay = 10 + (reset_time - time.time())
                    print('Waiting ' + delay + 's for rate limit to reset.')
                    time.sleep(delay)
                add_relationships(tigris_issue, gh_issue)
                time.sleep(1)


if __name__ == '__main__':
    main()
