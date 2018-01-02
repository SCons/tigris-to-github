""" Import tracker data from Tigris.org

This script needs the following steps to work:

1. Extract the issues as XML files via the project's xml.cgi:

    import_tigris.py files <tigris_project> <path to files dir>

   this will place all the downloaded XML files in the files dir.
   An example:

    import_tigris.py files scons import

2. Import the data via xmlrpc:

    import_tigris.py push <xmlrpc URL> <path to files dir>

   Example:

    import_tigris.py push http://admin:admin@localhost:8917/demo/xmlrpc import

And you're done!
"""
import requests

from urllib.request import urlopen
import sys
import os
import glob
import lxml
import lxml.etree
import base64
import xmlrpc
import csv

# ---------------------------------------------------------
# natsort: Natural string sorting.
# ---------------------------------------------------------

# By Seo Sanghyeon.  Some changes by Connelly Barnes.

def try_int(s):
    "Convert to integer if possible."
    try: return int(s)
    except: return s


def natsort_key(s):
    "Used internally to get a tuple by which s is sorted."
    import re
    return map(try_int, re.findall(r'(\d+|\D+)', s))


def natcmp(a, b):
    "Natural string comparison, case sensitive."
    return cmp(natsort_key(a), natsort_key(b))


def natcasecmp(a, b):
    "Natural string comparison, ignores case."
    return natcmp(a.lower(), b.lower())


def natsort(seq, cmp=natcmp):
    "In-place natural string sort."
    seq.sort(cmp)


def natsorted(seq, cmp=natcmp):
    "Returns a copy of seq, sorted by natural string sort."
    import copy
    temp = copy.copy(seq)
    natsort(temp, cmp)
    return temp

# ---------------------------------------------------------
# Download issues from Tigris
# ---------------------------------------------------------


def issue_exists(id, url):
    """ Return whether the issue page with the given
        index (1-based!) exists, or not.
        @param id Index (1-based) of the issue to test
        @param url Base URL to the project's xml.cgi (no params attached!)
        @return `True` if the issue exists, `False` if not
    """
    r = requests.get(url, params={'include_attachments': 'false', 'id': str(id)})
    issues_xml = lxml.etree.XML(bytes(r.text, encoding=r.encoding))
    for issue in issues_xml.xpath('issue'):
        error = issue.attrib.get('status_code', None)
        if error and error == "404":
            return False
        else:
            return True
    return False


def binprobe(left, right, index_exists):
    """ Searches the last existing entry in a
        "sequence of indices" from left to right (including).
        Assumes that "left" starts on an existing entry,
        and left <= right, and left >= 0, and right >= 0.
        The index "right" may either be the last existing entry,
        or points to an entry that doesn't exist.
        @param left Start index
        @param right End index
        @param index_exists Function that checks whether a 1-based index
                             is in or out of the sequence (exists or not).
        @return 1-based index of the last existing entry, in
                 the given interval
    """
    while ((right - left) > 1):
        middle = left + (right - left) // 2
        if not index_exists(middle):
            right = middle - 1
        else:
            left = middle

    # Special handling for when only the two
    # last IDs are left...or a single one (left=right).
    if index_exists(right):
        return right
    return left


def get_number_of_issues(url, start_id=1, BSEARCH_STEP_SIZE=1024):
    """ Return the 1-based index of the highest available (=existing)
        issue for the given base URL, when starting to
        probe at start_id.
        @param url Base URL to the project's xml.cgi (no params attached!)
        @param start_id Index (1-based) from where to probe upwards
        @return 1-based index of the last existing issue
    """
    # Start at the given index
    id = start_id
    # Loop in large steps, until id doesn't exist
    steps = 0
    while issue_exists(id, url):
        id += BSEARCH_STEP_SIZE
        steps += 1

    if steps:
        # Start the binary search
        left = id - BSEARCH_STEP_SIZE
        right = id - 1
        return binprobe(left, right,
                        lambda x: issue_exists(x, url))

    return id


def download_xmls_for_bugs(query_url, start_id, max_id, outdir, AT_A_TIME=50):
    print("Downloading XML data %d-%d to '%s'..." % (start_id, max_id, outdir))
    if AT_A_TIME < 1:
        AT_A_TIME = 1
    first_n = start_id
    rest = start_id + AT_A_TIME - 1
    file = 1
    while first_n <= max_id:
        # Ensure that no "overflow" at the end of the interval occurs
        if rest > max_id:
            rest = max_id
        print("%d-%d -> %02d.xml" % (first_n, rest, file))
        # Create a single URL to fetch all the bug data.
        if first_n < rest:
            ids = '%d-%d' % (first_n, rest)
        else:
            ids = '%d' % first_n
        r = requests.post(query_url, data={'download_filename': 'issues.xml', 'include_attachments': 'false', 'id': ids})
        with open(os.path.join(outdir, "%02d.xml" % file), "wb") as fout:
            fout.write(bytes(r.text, encoding=r.encoding))

        # Update the 'rest' of the work
        first_n += AT_A_TIME
        rest += AT_A_TIME
        file += 1

    print("\nDone.\n")


def fetch_files(project, outdir):
    # Ensure that the output directory exists
    if not os.path.isdir(outdir):
        os.makedirs(outdir)

    query_url = "http://%s.tigris.org/issues/xml.cgi" % project.lower()
    start_id = 1
    # Get the number of issues
    print("Probing ID of last issue...")
    max_id = get_number_of_issues(query_url, start_id)

    # Downloading information about those bugs.
    download_xmls_for_bugs(query_url, start_id, max_id, outdir)


def get_tag_text_from_xml(xml_doc, tag_name, index=0):
    """Given an object representing for example 
       <issue><tag>text</tag></issue>, and tag_name = 'tag',
       returns 'text'.
    """
    tags = xml_doc.xpath(tag_name)
    try:
        return tags[index].text or u''
    except IndexError:
        return ''

def reformat_date(date):
    return date.replace(" ", ".")

def expand_keywords(text):
    keywords = [s.strip() for s in text.split(',')]
    return keywords

def collect_users_and_keywords(xmlrpc_server, 
                                  issue, 
                                  user_map,
                                  keyword_map):
    new_users = {}
    new_keywords = {}
    
    # Collect reporter and assigned_to from issue
    reporter = get_tag_text_from_xml(issue, "reporter")
    if reporter and reporter not in user_map:
        new_users[reporter] = ['','']
    assigned_to = get_tag_text_from_xml(issue, "assigned_to")
    if assigned_to and assigned_to not in user_map:
        new_users[assigned_to] = ['','']
    # Collect user from issue/activity
    for activity in issue.xpath("activity"):
        user = get_tag_text_from_xml(activity, "user")
        if user and user not in user_map:
            new_users[user] = ['','']
    # Collect who from issue/long_desc
    for desc in issue.xpath("long_desc"):
        who = get_tag_text_from_xml(desc, "who")
        if who and who not in user_map:
            new_users[who] = ['','']
            
    # Add users
    for u in new_users:
        if u:
            new_users[u] = xmlrpc_server.create('user',
                                                'username=%s' % u,
                                                'roles=User')

    # Collect reporter and assigned_to from issue
    keys = expand_keywords(get_tag_text_from_xml(issue, "keywords"))
    for kw in keys:
        if kw and kw not in keyword_map:
            new_keywords[kw] = ''

    # Add keywords
    for k in new_keywords:
        if k:
            new_keywords[k] = xmlrpc_server.create('keyword',
                                                   'name=%s' % k)

    
    return new_users, new_keywords

    #
    # Database entries:
    #
    # {'username' : 'admin',
    #  'passwords' : '?',
    #  'roles' : 'Admin',
    #  'creator' : '',
    #  'creation' : '2014-05-04.20:58:15.604',
    #  'actor' : '?',
    #  'queries' : [],
    #  'activity' : '2014-05-04.20:58:15.604',
    #  'address' : 'roundup-admin@localhost'
    #  }


def create_file(xmlrpc_server, att, user_map):
    mimetype = get_tag_text_from_xml(att, "mimetype")
    date = get_tag_text_from_xml(att, "date")
    desc = get_tag_text_from_xml(att, "desc")
    ispatch = get_tag_text_from_xml(att, "ispatch")
    filename = get_tag_text_from_xml(att, "filename")
    submitter_id = get_tag_text_from_xml(att, "submitter_id")
    submitting_username = get_tag_text_from_xml(att, "submitting_username")
    data64based = get_tag_text_from_xml(att, "data")
    data = base64.b64decode(data64based)
    
    if not submitting_username:
        submitting_username = "unknown"
    
    print("Pushing file %s with length %d" % (filename, len(data)))
    return xmlrpc_server.create('file', 
                                'name=%s' % filename,
                                'type=%s' % mimetype,
                                xmlrpclib.Binary('content=%s' % data))

    #
    # Database entries:
    #
    # {'content' = data,
    #  'name' : 'scons.png',
    #  'creator' : '',
    #  'creation' : '2014-05-04.20:58:15.604',
    #  'actor' : '?',
    #  'activity' : '2014-05-04.20:58:15.604',
    #  'type' : 'image/png'
    #  }

def create_msg(xmlrpc_server, msg, user_map):
    who = get_tag_text_from_xml(msg, "who")
    date = get_tag_text_from_xml(msg, "issue_when")
    text = get_tag_text_from_xml(msg, "thetext")
    
    if not len(text):
        return None
    
    return xmlrpc_server.create('msg',
                                'author=%s' % user_map.get(who,'1'),
                                'date=%s' % reformat_date(date),
                                'content=%s' % text)

    #
    # Database entries:
    #
    #{'author': '3', 
    # 'summary': 'This is a first test', 
    # 'content': 'This is a first test', 
    # 'date': '<Date 2014-05-04.21:05:46.281>',
    # files = [],
    # recipients = [],
    # 'creation' : '2014-05-04.20:58:15.604',
    # 'actor' : '?',
    # 'activity' : '2014-05-04.20:58:15.604',
    # 'summary' : 'This is a first test',
    # 'creator' : '2014-05-04.20:58:15.604',
    # }

roundup_prio = {'critical' : 1,
                'urgent' : 2,
                'bug' : 3,
                'feature' : 4,
                'wish' : 5}

roundup_status = {'unread' : 1,
                  'deferred' : 2,
                  'chatting' : 3,
                  'need-eg' : 4,
                  'in-progress' : 5,
                  'testing' : 6,
                  'done-cbb' : 7,
                  'resolved' : 8}

tigris_prio = {'P1' : 1,
               'P2' : 2,
               'P3' : 3,
               'P4' : 4}

tigris_status = {'RESOLVED' : 1,
                 'CLOSED' : 2,
                 'VERIFIED' : 3,
                 'NEW' : 4,
                 'REOPENED' : 5,
                 'STARTED' : 6}

tigris_type = {'ENHANCEMENT' : 1,
               'DEFECT' : 2,
               'PATCH' : 3,
               'TASK' : 4}

def map_prio_and_status(issue_type, tprio, tstatus, resolution):
    # Default
    rprio = 3
    rstatus = 1
    
    if issue_type == 'ENHANCEMENT':
        rprio = roundup_prio['wish']
    if issue_type == 'TASK':
        rprio = roundup_prio['feature']
    
    if tstatus in ['STARTED', 'REOPENED']:
        rstatus = roundup_status['in-progress']
    if tstatus == 'VERIFIED':
        rstatus = roundup_status['chatting']
    if tstatus in ['RESOLVED', 'CLOSED']:
        rstatus = roundup_status['resolved']
    
    return rprio, rstatus


def timetuple_from_tigris_ts(tigris_ts):
    tl = tigris_ts.split()
    day = [str(int(x)) for x in tl[0].split('-')]
    tstamp = [str(int(x)) for x in tl[1].split(':')]
    return "(" + ", ".join(day + tstamp) + ", 0, 0, 0)"

def push_issue(xmlrpc_server, issue, user_map, keyword_map, fi, fm, ff):
    issue_id = get_tag_text_from_xml(issue, "issue_id")
    print("Id:", issue_id)
    
    # Create messages
    msglist = []
    for msg in issue.xpath("long_desc"):
        msgid = create_msg(xmlrpc_server, msg, user_map)
        if msgid:
            date = timetuple_from_tigris_ts(get_tag_text_from_xml(msg, "issue_when"))
            fm.write("%s;%s;%s\n" % (msgid, date, date))
            msglist.append(msgid)
    # Create files
    filelist = []
    for att in issue.xpath("attachment"):
        fileid = create_file(xmlrpc_server, att, user_map)
        if fileid:
            date = timetuple_from_tigris_ts(get_tag_text_from_xml(att, "date"))
            ff.write("%s;%s;%s\n" % (fileid, date, date))
            filelist.append(fileid)

    issue_status = get_tag_text_from_xml(issue, "issue_status")
    priority = get_tag_text_from_xml(issue, "priority")
    resolution = get_tag_text_from_xml(issue, "resolution")
    component = get_tag_text_from_xml(issue, "component")
    version = get_tag_text_from_xml(issue, "version")
    rep_platform = get_tag_text_from_xml(issue, "rep_platform")
    delta_ts = get_tag_text_from_xml(issue, "delta_ts")
    subcomponent = get_tag_text_from_xml(issue, "subcomponent")
    assigned_to = get_tag_text_from_xml(issue, "assigned_to")
    issue_type = get_tag_text_from_xml(issue, "issue_type")
    reporter = get_tag_text_from_xml(issue, "reporter")
    target_milestone = get_tag_text_from_xml(issue, "target_milestone")
    creation_ts = get_tag_text_from_xml(issue, "creation_ts")
    qa_contact = get_tag_text_from_xml(issue, "qa_contact")
    op_sys = get_tag_text_from_xml(issue, "op_sys")
    short_desc = get_tag_text_from_xml(issue, "short_desc")
    keywords = expand_keywords(get_tag_text_from_xml(issue, "keywords"))

    rprio, rstatus = map_prio_and_status(issue_type, priority, issue_status, resolution)
    clist = [
             'title=%s' % short_desc,
             'priority=%d' % rprio,
             'status=%d' % rstatus,
             'assignedto=%s' % user_map.get(assigned_to,'1')
             ]
    
    if keywords:
        clist.append('keyword=%s' % ','.join(keywords))
    if filelist:
        clist.append('files=%s' % ','.join(filelist))
    if msglist:
        clist.append('messages=%s' % ','.join(msglist))
                     
    xmlrpc_server.create('issue', *clist)
    fi.write("%s;%s;%s\n" % (issue_id, timetuple_from_tigris_ts(creation_ts), timetuple_from_tigris_ts(delta_ts)))
    
    #
    # Database entries:
    #
    # {'status' : '3',
    #  'files' : ['1'],
    #  'keyword' : ['habba', 'gully'],
    #  'title' : 'Test issue',
    #  'nosy' : [],
    #  'messages' : ['1', '2'], 
    #  'creation' : '2014-05-04.20:58:15.604',
    #  'actor' : '?',
    #  'priority' : '3',
    #  'superseder' : [],
    #  'assignedto' : 'stevenknight'
    #  'activity' : '2014-05-04.20:58:15.604',
    #  'creator' : '2014-05-04.20:58:15.604'
    #  
    #  }


def import_xml(xmlrpc_url, file_dir):
    """ Generate Roundup tracker import files based on the tracker schema,
    sf.net xml export and downloaded files from sf.net.
    
    http://demo:demo@localhost:8917/demo/xmlrpc
    """
    
    # Change into folder with XML files
    oldwd = os.path.abspath(os.getcwd())
    os.chdir(file_dir)
    # Get list of XML files
    xfiles = glob.glob('*.xml')
    
    if xfiles:
        # Open connection to XMLRPC server
        xmlrpc_server = xmlrpclib.ServerProxy(xmlrpc_url, allow_none=True)
        
        # Process files in order
        user_map = {}
        keyword_map = {}

        # Try to read fullnames.csv for names and roles of project members
        with open('fullnames.csv', 'rb') as fnames:
            reader = csv.reader(fnames, delimiter=";")
            cnt = 0
            for row in reader:
                if cnt > 0:
                    user_map[row[0]] = xmlrpc_server.create('user',
                                                            'username=%s' % row[0],
                                                            'realname=%s' % row[1],
                                                            'roles=User')  # %s' % row[2])
                cnt += 1
            print("Added %d users from fullname.csv." % (cnt-1))

        # Collect users
        print("Pushing keywords and user IDs...")
        for fpath in natsorted(xfiles):
            with open(fpath, "r") as f:
                try:
                    issues_xml = lxml.etree.XML(f.read())
                    for issue in issues_xml.xpath('issue'):
                        error = issue.attrib.get('status_code', None)
                        if error and error == "404":
                            continue  # skip empty issues
                        
                        new_users, new_keywords = collect_users_and_keywords(xmlrpc_server, 
                                                  issue,
                                                  user_map,
                                                  keyword_map)
                        # Update maps
                        user_map.update(new_users)
                        keyword_map.update(new_keywords)
                except:
                    pass
        
        print("Users: %d" % len(user_map))
        print("Keywords: %d\n" % len(keyword_map))
        # Push files, messages and issues
        print("Pushing ALL the things!")
        # Open files for keeping track of the original creation and "last
        # activity" dates...these are used for the "patch" stage (see below).
        fi = open("issue_dates.csv","w")
        fm = open("msg_dates.csv","w")
        ff = open("file_dates.csv","w")
        for fpath in natsorted(xfiles):
            with open(fpath, "r") as f:
                try:
                    issues_xml = lxml.etree.XML(f.read())
                    for issue in issues_xml.xpath('issue'):
                        error = issue.attrib.get('status_code', None)
                        if error and error == "404":
                            continue  # skip empty issues
                         
                        push_issue(xmlrpc_server, issue, user_map, keyword_map, fi, fm, ff)
                except:
                    pass
        fi.close()
        fm.close()
        ff.close()
    
    # Change back to original directory
    os.chdir(oldwd)

def read_dates(fpath, date_dict):
    """ Reads the info CSV file with the original creation
        and "last activity" times and stores them into the
        dictionary date_dict.
    """
    with open(fpath, 'rb') as f:
        reader = csv.reader(f, delimiter=";")
        for row in reader:
            date_dict[row[0]] = [row[1], row[2]]

def patch_roundup_file(fpath, date_dict, touched_col, created_col, id_col):
    """ Patches the given Roundup CSV file fpath, by replacing the created
        and "last activity" dates with the original times from the Tigris import,
        as stored in the dictionary date_dict.
    """
    # Read data and compile new output file
    content = []
    # We try to keep as much of the rest of the line in original form as possible,
    # which is important for messages in particular.
    cutoff_col = max(touched_col, created_col)+1
    with open(fpath, 'rb') as f:
        cnt = 0
        for line in f.readlines():
            line = line.rstrip('\n')
            row = line.split(':')
            # Is it a proper data row?
            if cnt > 0 and len(row) > 1:
                # Try to find ID in dict
                item_id = row[id_col].strip("'")
                if item_id in date_dict:
                    # Set new time of creation
                    row[created_col] = date_dict[item_id][0]
                    # Set new time of last activity
                    row[touched_col] = date_dict[item_id][1]
                    # Split line at cutoff_col
                    colon = 1
                    pos = line.find(":")
                    while colon < cutoff_col:
                        pos = line.find(":", pos+1)
                        colon += 1
                    content.append(":".join(row[:cutoff_col]) + line[pos:])
                else:
                    content.append(line)
            else:
                content.append(line)
            cnt += 1

    # Write new content to same filename
    with open(fpath, "w") as fout:
        fout.write("\n".join(content))


def patch_files(roundup_dir, file_dir):
    """ Patch Roundup tracker export files based on the recorded IDs,
    and created/last activity infos as tracked during the Tigris import.
    """
    
    # Change into folder with Tigris XML files
    oldwd = os.path.abspath(os.getcwd())
    os.chdir(file_dir)
    # Get list of CSV files with created/touched infos
    xfiles = glob.glob('*.csv')
    
    date_files = {}
    date_issues = {}
    date_messages = {}
    if xfiles:
        # Process CSV files
        print("Reading CSV files")
        read_dates("issue_dates.csv", date_issues)
        read_dates("msg_dates.csv", date_messages)
        read_dates("file_dates.csv", date_files)

    print("Issues: %d" % len(date_issues))
    print("Messages: %d" % len(date_messages))
    print("Files: %d" % len(date_files))
        
    # Change back to original directory
    os.chdir(oldwd)
    # Change into folder with Roundup export files
    os.chdir(roundup_dir)

    # Patch exported CSV files
    print("Patching Roundup files")
    patch_roundup_file("file.csv", date_files, touched_col=0, created_col=2, id_col=4)
    patch_roundup_file("issue.csv", date_issues, touched_col=0, created_col=3, id_col=6)
    patch_roundup_file("msg.csv", date_messages, touched_col=0, created_col=3, id_col=7)

    # Change back to original directory
    os.chdir(oldwd)

def usage():
    print("Usage: import_tigris.py <push|files|patch> options")
    print("For 'push' give the URL of the xmlrpc server and the XML directory, e.g. http://demo:demo@localhost:8917/demo/xmlrpc tigris_export")
    print("For 'files' give the project name and the output directory, e.g. scons tigris_export")
    print("For 'patch' give the roundup export folder and the XML output directory, e.g. roundup_export tigris_export")

def main():
    if len(sys.argv) < 2:
        usage()
        sys.exit(0)
    if sys.argv[1] == 'push':
        import_xml(*sys.argv[2:])
    elif sys.argv[1] == 'files':
        fetch_files(*sys.argv[2:])
    elif sys.argv[1] == 'patch':
        patch_files(*sys.argv[2:])

if __name__ == '__main__':
    main()
