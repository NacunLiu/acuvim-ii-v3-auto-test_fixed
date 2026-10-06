"""Minimal Jira Cloud REST client for the BACnet monitoring job.

Credentials come from ~/.jira.json ({"site", "email", "token"}) -- never from
the command line, the environment of a shared machine, or this source file.
See the Credentials section of the global CLAUDE.md.

    python -m scripts.jira_post whoami
    python -m scripts.jira_post get RT-583
    python -m scripts.jira_post comment RT-583 --text "..."
    python -m scripts.jira_post comment RT-583 --file body.md
    python -m scripts.jira_post attach  RT-583 evidence.png

Comments are posted as Atlassian Document Format; --text/--file is plain text,
one paragraph per blank-line-separated block.
"""
import argparse
import json
import os
import re
import sys

import requests

CONFIG = os.path.expanduser('~/.jira.json')
TIMEOUT = 30


def _load():
    try:
        with open(CONFIG, encoding='utf-8-sig') as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        sys.exit('missing {} -- see CLAUDE.md'.format(CONFIG))
    except ValueError as e:
        sys.exit('{} is not valid JSON: {}'.format(CONFIG, e))
    missing = [k for k in ('site', 'email', 'token') if not str(cfg.get(k, '')).strip()]
    if missing:
        sys.exit('{} is missing: {}'.format(CONFIG, ', '.join(missing)))
    return cfg['site'].rstrip('/'), cfg['email'], cfg['token']


def _call(method, path, **kw):
    site, email, token = _load()
    headers = {'Accept': 'application/json'}
    headers.update(kw.pop('headers', None) or {})
    r = requests.request(method, site + path, auth=(email, token),
                         headers=headers, timeout=TIMEOUT, **kw)
    if not r.ok:
        # Jira echoes the request on 4xx; print only its error list, never auth.
        try:
            detail = r.json().get('errorMessages') or r.json().get('errors')
        except ValueError:
            detail = r.text[:300]
        sys.exit('HTTP {} on {} {}\n{}'.format(r.status_code, method, path, detail))
    return r.json() if r.content else {}


def _media_uuid(attachment_id):
    """Jira's ADF needs the media UUID, not the numeric attachment id.

    There is no field for it: the only reliable source is the 303 that
    /attachment/content/{id} issues towards api.media.atlassian.com, whose path
    carries the UUID.
    """
    site, email, token = _load()
    r = requests.get('{}/rest/api/3/attachment/content/{}'.format(site, attachment_id),
                     auth=(email, token), timeout=TIMEOUT, allow_redirects=False)
    loc = r.headers.get('Location', '')
    m = re.search(r'/file/([0-9a-f-]{36})', loc)
    if not m:
        sys.exit('could not resolve a media UUID for attachment {}'.format(attachment_id))
    return m.group(1)


def _image_node(path, attachment_id):
    uuid = _media_uuid(attachment_id)
    attrs = {'type': 'file', 'id': uuid, 'alt': os.path.basename(path), 'collection': ''}
    try:
        from PIL import Image
        with Image.open(path) as im:
            attrs['width'], attrs['height'] = im.size
    except Exception:
        pass
    return {'type': 'mediaSingle', 'attrs': {'layout': 'align-start'},
            'content': [{'type': 'media', 'attrs': attrs}]}


def _upload(key, path):
    with open(path, 'rb') as fh:
        d = _call('POST', '/rest/api/3/issue/{}/attachments'.format(key),
                  headers={'X-Atlassian-Token': 'no-check'},
                  files={'file': (os.path.basename(path), fh)})
    att = d[0] if isinstance(d, list) else d
    return att['id'], att['filename']


def _adf(text):
    """Plain text -> Atlassian Document Format, blank lines splitting paragraphs."""
    blocks = [b.strip() for b in text.replace('\r\n', '\n').split('\n\n') if b.strip()]
    return {
        'type': 'doc', 'version': 1,
        'content': [{'type': 'paragraph',
                     'content': [{'type': 'text', 'text': b}]} for b in blocks],
    }


def cmd_whoami(_):
    me = _call('GET', '/rest/api/3/myself')
    print('authenticated as {} <{}>  accountId={}  active={}'.format(
        me.get('displayName'), me.get('emailAddress'), me.get('accountId'), me.get('active')))


def cmd_get(args):
    d = _call('GET', '/rest/api/3/issue/{}'.format(args.key),
              params={'fields': 'summary,status,assignee,resolution,updated'})
    f = d['fields']
    print('{}  {}'.format(d['key'], f['summary']))
    print('   status     : {}'.format((f.get('status') or {}).get('name')))
    print('   resolution : {}'.format((f.get('resolution') or {}).get('name') or '-'))
    print('   assignee   : {}'.format((f.get('assignee') or {}).get('displayName') or '-'))
    print('   updated    : {}'.format(f.get('updated')))


def cmd_comment(args):
    body = open(args.file, encoding='utf-8').read() if args.file else args.text
    if not body or not body.strip():
        sys.exit('refusing to post an empty comment')
    if args.dry_run:
        print('--- would {} on {} ---'.format(
            'update comment ' + args.update if args.update else 'post to', args.key))
        print(body)
        for img in args.image or []:
            print('[embedded image: {}]'.format(img))
        return

    doc = _adf(body)
    for img in args.image or []:
        att_id, _ = _upload(args.key, img)
        doc['content'].append(_image_node(img, att_id))

    hdr = {'Accept': 'application/json', 'Content-Type': 'application/json'}
    payload = json.dumps({'body': doc})
    if args.update:
        d = _call('PUT', '/rest/api/3/issue/{}/comment/{}'.format(args.key, args.update),
                  headers=hdr, data=payload)
    else:
        d = _call('POST', '/rest/api/3/issue/{}/comment'.format(args.key),
                  headers=hdr, data=payload)
    site, _, _ = _load()
    print('{} comment {} -> {}/browse/{}?focusedCommentId={}'.format(
        'updated' if args.update else 'posted', d.get('id'), site, args.key, d.get('id')))


def cmd_attach(args):
    """Upload a file to the issue. Jira requires the XSRF opt-out header here."""
    att_id, name = _upload(args.key, args.path)
    print('attached {}  id={}'.format(name, att_id))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('whoami').set_defaults(fn=cmd_whoami)
    g = sub.add_parser('get'); g.add_argument('key'); g.set_defaults(fn=cmd_get)
    c = sub.add_parser('comment')
    c.add_argument('key')
    src = c.add_mutually_exclusive_group(required=True)
    src.add_argument('--text')
    src.add_argument('--file')
    c.add_argument('--image', action='append',
                   help='attach and embed an image; repeatable')
    c.add_argument('--update', metavar='COMMENT_ID',
                   help='rewrite this existing comment instead of posting a new one')
    c.add_argument('--dry-run', action='store_true')
    c.set_defaults(fn=cmd_comment)
    a = sub.add_parser('attach')
    a.add_argument('key'); a.add_argument('path'); a.set_defaults(fn=cmd_attach)
    args = ap.parse_args()
    args.fn(args)


if __name__ == '__main__':
    main()
