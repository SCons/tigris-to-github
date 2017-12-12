#!/bin/env/python

import base64
import getpass
import glob
import html
import json
import tempfile

from github import Github, UnknownObjectException
import lxml
import lxml.etree
import requests

import import_tigris


def add_label_if_not_default(tigris_issue, gh_issue, field_name, default_value):
    field_value = tigris_issue.xpath(field_name)[0].text
    if field_value and (field_value != default_value):
        gh_issue.add_to_labels(field_name.replace(
            '_', ' ').title() + ': ' + field_value)


def import_issue_status(tigris_issue, gh_issue):
    '''Map between Tigris issue status and the states on GitHub.'''
    status = tigris_issue.xpath('issue_status')[0].text
    state = 'closed' if status in (
        'RESOLVED', 'CLOSED', 'VERIFIED') else 'open'
    gh_issue.edit(state=state)


def import_priority(tigris_issue, gh_issue):
    '''Import the issue's priority, if set.'''
    priority = tigris_issue.xpath('priority')[0].text
    if priority:
        gh_issue.add_to_labels()


def import_resolution(tigris_issue, gh_issue):
    '''Map between Tigris resolutions and the default labels on GitHub.'''
    resolution_map = {
        'DUPLICATE': 'duplicate',
        'INVALID': 'invalid',
        'WONTFIX': 'wontfix'
    }
    label = resolution_map.get(tigris_issue.xpath('resolution')[0].text)
    if label:
        gh_issue.add_to_labels(label)


def import_component(tigris_issue, gh_issue):
    add_label_if_not_default(tigris_issue, gh_issue, 'component', 'scons')


def import_version(tigris_issue, gh_issue):
    add_label_if_not_default(tigris_issue, gh_issue,
                             'version', '-unspecified-')


def import_rep_platform(tigris_issue, gh_issue):
    add_label_if_not_default(tigris_issue, gh_issue, 'rep_platform', 'All')


def import_assigned_to(tigris_issue, gh_issue):
    pass


def import_subcomponent(tigris_issue, gh_issue):
    add_label_if_not_default(tigris_issue, gh_issue, 'subcomponent', 'scons')


def import_reporter(tigris_issue, gh_issue):
    '''Prefix the issue's description with the reporter.'''
    reporter = tigris_issue.xpath('reporter')[0].text
    if reporter:
        gh_issue.edit(body='This issue was reported by: `' +
                      reporter + '`.\r\n' + gh_issue.body)


def import_target_milestone(tigris_issue, gh_issue):
    '''Associate the GitHub issue with a milestone if the Tigris issue has one.'''
    tigris_milestone = tigris_issue.xpath('target_milestone')[0].text
    if tigris_milestone != '-unspecified-':
        milestone = None
        for m in gh_issue.repository.get_milestones():
            if m.title == tigris_milestone:
                milestone = m
                break
        if not milestone:
            milestone = gh_issue.repository.create_milestone(
                tigris_milestone, description="Created automatically")
        gh_issue.edit(milestone=milestone)


def import_issue_type(tigris_issue, gh_issue):
    '''Map between Tigris issue_type and the default labels on GitHub.'''
    type_map = {
        'DEFECT': 'bug',
        'ENHANCEMENT': 'enhancement'
    }
    label = type_map.get(tigris_issue.xpath('issue_type')[0].text)
    if label:
        gh_issue.add_to_labels(label)


def import_creation_ts(tigris_issue, gh_issue):
    creation_ts = tigris_issue.xpath('creation_ts')[0].text
    gh_issue.edit(body='This issue was originally created at: ' +
                  creation_ts + '.\r\n' + gh_issue.body)


def import_issue_file_loc(tigris_issue, gh_issue):
    '''optional'''
    issue_file_loc = tigris_issue.xpath('issue_file_loc')
    if issue_file_loc and issue_file_loc[0].text:
        gh_issue.edit(body=gh_issue.body + '\r\nMore information about this issue is at ' +
                      issue_file_loc[0].text + '.\r\n')


def import_votes(tigris_issue, gh_issue):
    '''optional'''
    votes = tigris_issue.xpath('votes')
    if votes and votes[0].text:
        gh_issue.edit(body=gh_issue.body +
                      '\r\nVotes for this issue: ' + votes[0].text + '.\r\n')


def import_op_sys(tigris_issue, gh_issue):
    add_label_if_not_default(tigris_issue, gh_issue, 'op_sys', 'All')


def import_keywords(tigris_issue, gh_issue):
    '''Add a label for each keyword.'''
    # There can be many keywords fields, each containing comma-separated values.
    if len(tigris_issue.xpath('keywords')) > 0:
        labels = []
        for keywords in tigris_issue.xpath('keywords'):
            for keyword in keywords:
                if keyword.text:
                    labels.append(keyword.text.split(',').strip())
        if labels:
            gh_issue.add_to_labels(labels)


def add_relationship(tigris_issue, gh_issue, field_name, relationship):
    suffix = ''
    field_name = 'dependson'
    sorted_fields = sorted(tigris_issue.xpath(
        field_name), key=lambda x: x.xpath('when')[0].text)
    for field in sorted_fields:
        suffix += '\r\n' + field.xpath('who')[0].text
        suffix += ' said this issue ' + relationship + ' #'
        suffix += field.xpath('issue_id')[0].text
        suffix += ' at ' + field.xpath('when')[0].text + '.\r\n'
    if suffix:
        gh_issue.edit(body=gh_issue.body + suffix)


def import_dependson(tigris_issue, gh_issue):
    '''*'''
    add_relationship(tigris_issue, gh_issue, 'dependson', 'depends on')


def import_blocks(tigris_issue, gh_issue):
    '''*'''
    add_relationship(tigris_issue, gh_issue, 'blocks', 'blocks')


def import_is_duplicate(tigris_issue, gh_issue):
    '''*'''
    add_relationship(tigris_issue, gh_issue,
                     'is_duplicate', ' is a duplicate of')


def import_has_duplicates(tigris_issue, gh_issue):
    '''*'''
    add_relationship(tigris_issue, gh_issue,
                     'has_duplicates', 'is duplicated by')


def import_attachment(tigris_issue, gh_issue, user, passwd, attachment_repo):
    '''PyGithub doesn't support the Contents endpoint of the GitHub REST API
    https://developer.github.com/v3/repos/contents/.
    '''
    suffix = ''
    url_prefix = '/'.join(('https://api.github.com/repos', user, attachment_repo, 'contents'))
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

        suffix += '\r\n' + who
        suffix += ' attached [' + filename + '](' + dest_url + ')'
        suffix += ' at ' + attachment.xpath('date')[0].text + '.\r\n'
        desc = attachment.xpath('desc')[0].text
        if desc:
            suffix += '>' + desc + '\r\n'

        # Copy the attachment to a temporary file, and upload to GitHub.
        src_url = attachment.xpath('attachment_iz_url')[0].text
        r = requests.get(src_url, stream=True)
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
        gh_issue = repo.create_issue(title)
        if gh_issue.number != issue_id:
            print(issue_id, gh_issue.number)
            # Someone's created an issue whilst we working, overwrite theirs.
            gh_issue = repo.get_issue(issue_id)

    # Create the initial body of the issue.
    body = ''
    sorted_long_descs = sorted(tigris_issue.xpath(
        'long_desc'), key=lambda x: x.xpath('issue_when')[0].text)
    for long_desc in sorted_long_descs:
        body += long_desc.xpath('who')[0].text
        body += ' said at '
        body += long_desc.xpath('issue_when')[0].text
        body += '\r\n>' + html.unescape(long_desc.xpath('thetext')[0].text)
        body += '\r\n\r\n'
    gh_issue.edit(title=title, body=body)

    # Each function maps to a field in the Tigris issue, based on the DTD at
    # http://scons.tigris.org/issues/issuezilla.dtd
    import_issue_status(tigris_issue, gh_issue)
    import_priority(tigris_issue, gh_issue)
    import_resolution(tigris_issue, gh_issue)
    import_component(tigris_issue, gh_issue)
    import_version(tigris_issue, gh_issue)
    import_rep_platform(tigris_issue, gh_issue)
    import_assigned_to(tigris_issue, gh_issue)
    import_subcomponent(tigris_issue, gh_issue)
    import_reporter(tigris_issue, gh_issue)
    import_target_milestone(tigris_issue, gh_issue)
    import_issue_type(tigris_issue, gh_issue)
    import_creation_ts(tigris_issue, gh_issue)
    import_issue_file_loc(tigris_issue, gh_issue)
    import_votes(tigris_issue, gh_issue)
    import_op_sys(tigris_issue, gh_issue)
    import_keywords(tigris_issue, gh_issue)

    import_attachment(tigris_issue, gh_issue, user, passwd, attachment_repo)


def update_issue_dependencies(tigris_issue, gh_issue):
    '''This requires all of the issues to have been created.'''
    import_dependson(tigris_issue, gh_issue)
    import_blocks(tigris_issue, gh_issue)
    import_is_duplicate(tigris_issue, gh_issue)
    import_has_duplicates(tigris_issue, gh_issue)


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
    issue_repo = gh.get_user().get_repo(input("GitHub repository for issues: "))
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
                print(tigris_issue.xpath('issue_id')[0].text)
                import_to_github(tigris_issue, issue_repo, user, passwd, attachment_repo)
    # Now all the issues are in imported add the relationships between them.
    for issue_group_file in glob.glob('xml/*.xml'):
        with open(issue_group_file, 'rb') as f_in:
            issues_xml = lxml.etree.XML(f_in.read())
            for tigris_issue in issues_xml:
                update_issue_dependencies(tigris_issue, issue_repo)


if __name__ == '__main__':
    main()
