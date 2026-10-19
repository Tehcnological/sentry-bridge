"""Exercise the PowerShell publication gates using a local fake gh, without network writes."""
import base64
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling test modules, when run on their own
import test_release_bundle as fixtures
import release_bundle as rb

FAKE_GH = r'''
import hashlib,json,os,re,sys
from pathlib import Path
root=Path(os.environ['SB_FAKE_GH_ROOT'])
state_path=root/'state.json'
state=json.loads(state_path.read_text()) if state_path.exists() else {}
args=sys.argv[1:]
mode=os.environ.get('SB_FAKE_GH_MODE','success')
if 'releases' not in state:
    # existing-draft: an earlier, unpublished draft for the same tag is already on GitHub;
    # page-2-draft puts it after 100 unrelated releases, beyond the first page.
    old=[{'id':i,'tag_name':f'v0.0.{i}','draft':False,'assets':[]} for i in range(1000,1100)]
    draft=[{'id':7,'tag_name':'v0.1.3','draft':True,'assets':[]}]
    state['releases']={'existing-draft':draft,'page-2-draft':old+draft,'page-2-fail':old}.get(mode,[])
    # existing-*-tag: the tag is already on GitHub before the run, at another or the same commit.
    state['tags']={'v0.1.3':'b'*40} if mode=='existing-other-tag' else {'v0.1.3':'a'*40} if mode=='existing-annotated-tag' else {}
releases=state['releases']
tags=state['tags']
if args[0]=='api':
    method,rest='GET',args[1:]
    if rest[0]=='-X': method,rest=rest[1],rest[2:]
    endpoint=rest[0]
    if '/releases/tags/' in endpoint:
        # Like GitHub: lookup by tag never returns a draft.
        match=[r for r in releases if r['tag_name']==endpoint.rsplit('/',1)[1] and not r['draft']]
        if not match:
            print('gh: Not Found (HTTP 404)',file=sys.stderr); sys.exit(1)
        print(json.dumps(match[0]))
    elif endpoint.endswith('/releases'):
        if method!='GET' or len(rest)%2==0 or rest[1::2]!=['-f']*(len(rest)//2): sys.exit(8)
        query=dict(field.split('=',1) for field in rest[2::2])
        size,page=int(query.get('per_page',30)),int(query.get('page',1))
        state.setdefault('pages',[]).append(page)
        rows=releases[(page-1)*size:page*size]
        # late-draft: the first listing after the upload does not show the new draft yet.
        if state.pop('hide-drafts',False): rows=[r for r in rows if not r['draft']]
        state_path.write_text(json.dumps(state))
        if mode=='page-2-fail' and page==2:
            print('gh: Server Error (HTTP 502)',file=sys.stderr); sys.exit(1)
        print(json.dumps(rows))
    elif re.search(r'/releases/\d+$',endpoint):
        release=next(r for r in releases if r['id']==int(endpoint.rsplit('/',1)[1]))
        if method=='PATCH':
            if rest[1:]!=['-F','draft=false']: sys.exit(8)
            release['draft']=False
            state['published']=True
            # Like GitHub: publishing creates a missing tag at the draft's target.
            tags.setdefault(release['tag_name'],'b'*40 if mode=='wrong-tag-commit' else release['target_commitish'])
            # late-tag: the new tag is not visible to the first lookup after publishing.
            if mode=='late-tag': state['hide-tag']=True
        print(json.dumps(release))
    elif '/git/ref/tags/' in endpoint:
        tag=endpoint.rsplit('/',1)[1]
        state.setdefault('tag-lookups',0)
        state['tag-lookups']+=1
        # tag-mid-run: someone pushes the tag at another commit after the first check.
        if mode=='tag-mid-run' and state['tag-lookups']==2: tags[tag]='b'*40
        hidden=state.pop('hide-tag',False)
        state_path.write_text(json.dumps(state))
        if tag not in tags or hidden:
            print('gh: Not Found (HTTP 404)',file=sys.stderr); sys.exit(1)
        # annotated-tag: the ref points to a tag object, which points to the commit.
        if mode in ('annotated-tag','existing-annotated-tag'): print(json.dumps({'object':{'type':'tag','sha':'c'*40}}))
        else: print(json.dumps({'object':{'type':'commit','sha':tags[tag]}}))
    elif '/git/tags/' in endpoint:
        if not endpoint.endswith('c'*40): sys.exit(9)
        print(json.dumps({'object':{'type':'commit','sha':tags['v0.1.3']}}))
    elif '/git/trees/' in endpoint:
        if mode=='stderr-notice': print('A new release of gh is available.',file=sys.stderr)
        print((root/'tree.json').read_text())
    elif '/compare/' in endpoint:
        if mode=='stderr-notice': print('A new release of gh is available.',file=sys.stderr)
        print(json.dumps({'status':'diverged' if mode=='unmerged' else 'ahead'}))
    elif '/contents/.github/workflows?' in endpoint:
        if mode=='no-workflows': print('gh: Not Found (HTTP 404)',file=sys.stderr); sys.exit(1)
        if mode=='workflows-error': print('gh: Server Error (HTTP 502)',file=sys.stderr); sys.exit(1)
        names=['tests.yml']+(['other.yml'] if mode=='extra-workflow' else [])
        print(json.dumps([{'type':'file','name':n} for n in names]+[{'type':'dir','name':'nested'}]))
    elif '/contents/.github/workflows/' in endpoint:
        bad=mode=='automatic' or (mode=='extra-workflow' and '/other.yml' in endpoint)
        print((root/('bad-workflow.json' if bad else 'workflow.json')).read_text())
    else: sys.exit(9)
elif args[:2]==['release','create']:
    state['created']=True
    state['assets']=[]
    for arg in args:
        p=Path(arg)
        if p.is_file() and p.parent.name=='v0.1.3':
            digest=hashlib.sha256(p.read_bytes()).hexdigest()
            if mode=='bad-digest': digest='0'*64
            state['assets'].append({'name':p.name,'size':p.stat().st_size,'digest':'sha256:'+digest})
    target=args[args.index('--target')+1]
    # changed-target: the draft as GitHub holds it targets another commit than requested.
    draft=dict(tag_name=args[2],draft=True,prerelease='--prerelease' in args,assets=state['assets'],target_commitish='b'*40 if mode=='changed-target' else target)
    releases.append(dict(draft,id=4242))
    # ambiguous-draft: a second draft for the tag appeared while uploading.
    if mode=='ambiguous-draft': releases.append(dict(draft,id=4243))
    if mode=='late-draft': state['hide-drafts']=True
else: sys.exit(9)
state_path.write_text(json.dumps(state))
'''


@unittest.skipUnless(shutil.which('pwsh'), 'PowerShell 7 (pwsh) is required for the publication tool tests')
class TestPublishRelease(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.TestReleaseBundle(methodName='test_complete_bundle_and_matching_commit')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root
        (self.root / 'tools').mkdir()
        for name in ('Publish-Release.ps1', 'Native-Command.ps1', 'Local-Paths.ps1', 'release_bundle.py'):
            shutil.copyfile(ROOT / 'tools' / name, self.root / 'tools' / name)
        # pwsh wraps error output to the console width; report the plain message instead.
        (self.root / 'tools/Run-Publisher.ps1').write_text(
            'try { & (Join-Path $PSScriptRoot Publish-Release.ps1) @args }\n'
            'catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }\n')
        fixture.tests['sources'] = rb.source_snapshot(self.root)
        fixture.apk['sources'] = rb.source_snapshot(self.root)
        fixture.write_reports()
        rb.prepare(self.root, 'v0.1.3')
        tools = self.root / '.local-tools'
        tools.mkdir()
        (tools / 'windows-environment.json').write_text(json.dumps({'python':sys.executable}))
        fake = tools / 'fake-gh'
        fake.mkdir()
        (fake / 'fake_gh.py').write_text(FAKE_GH)
        if sys.platform == 'win32':
            (fake / 'gh.cmd').write_text(f'@"{sys.executable}" "{fake / "fake_gh.py"}" %*\n')
        else:
            shim = fake / 'gh'
            shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake / "fake_gh.py"}" "$@"\n')
            shim.chmod(0o755)
        (fake / 'tree.json').write_text(json.dumps({'tree':[
            {'type':'blob','path':p,'sha':s} for p,s in rb.source_snapshot(self.root).items()]}))
        for name,text in (('workflow','on:\n  workflow_dispatch:\n'), ('bad-workflow','on:\n  push:\n')):
            (fake / (name+'.json')).write_text(json.dumps({'content':base64.b64encode(text.encode()).decode()}))
        (self.root / 'notes.md').write_text('Simulated publication test.\n')
        self.fake = fake
        # The fixture's own tool folder: an inherited SB_LOCAL_TOOLS (Test-Local sets it) would point
        # the publisher at a real clone's folder.
        self.environment = dict(os.environ, SB_FAKE_GH_ROOT=str(fake), PATH=str(fake)+os.pathsep+os.environ['PATH'],
                                SB_LOCAL_TOOLS=str(tools), SB_SIGNING_DIR=str(self.root/'local-signing'))
        self.pwsh = shutil.which('pwsh')

    def run_publisher(self, mode, commit='a'*40, extra=()):
        result = subprocess.run([self.pwsh,'-NoProfile','-File',str(self.root/'tools/Run-Publisher.ps1'),
            '-Tag','v0.1.3','-Commit',commit,'-NotesFile',str(self.root/'notes.md'),*extra],
            env=dict(self.environment, SB_FAKE_GH_MODE=mode), capture_output=True, text=True, timeout=60)
        state_path = self.fake / 'state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        return result, state

    def test_publishes_only_after_complete_verified_upload(self):
        tools_before = sorted(p.name for p in (self.root/'.local-tools').iterdir())
        result,state = self.run_publisher('success')
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        # Scratch files stay in build\local; the tool folder is only read.
        self.assertEqual(sorted(p.name for p in (self.root/'.local-tools').iterdir()), tools_before)
        self.assertTrue((self.root/'build/local/release-source-tree.json').is_file())
        self.assertTrue(state.get('created'))
        self.assertTrue(state.get('published'))
        self.assertEqual(len(state['assets']),len(rb.DISTRIBUTION_ASSETS)+2)  # plus SHA256SUMS and the manifest
        # The verified draft was published by its release ID, and nothing else changed.
        self.assertEqual([(r['id'],r['draft']) for r in state['releases']],[(4242,False)])
        self.assertEqual(state['tags'],{'v0.1.3':'a'*40})
        self.assertFalse(state['releases'][0]['prerelease'])

    def test_prerelease_switch_marks_a_plain_tag(self):
        result,state = self.run_publisher('success', extra=('-Prerelease',))
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertTrue(state.get('published'))
        self.assertTrue(state['releases'][0]['prerelease'])

    def test_publishes_when_an_annotated_tag_points_to_the_commit(self):
        result,state = self.run_publisher('annotated-tag')
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertTrue(state.get('published'))

    def test_existing_annotated_tag_at_the_commit_is_used(self):
        result,state = self.run_publisher('existing-annotated-tag')
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertTrue(state.get('published'))

    def test_existing_tag_at_another_commit_prevents_upload(self):
        result,state = self.run_publisher('existing-other-tag')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Existing tag points to a different commit',result.stderr)
        self.assertFalse(state.get('created'))

    def test_tag_pushed_during_upload_is_not_published(self):
        result,state = self.run_publisher('tag-mid-run')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('The tag now points to a different commit',result.stderr)
        self.assertTrue(state.get('created'))
        self.assertFalse(state.get('published'))

    def test_late_tag_and_late_draft_are_waited_for(self):
        for mode in ('late-tag','late-draft'):
            with self.subTest(mode=mode):
                (self.fake / 'state.json').unlink(missing_ok=True)
                result,state = self.run_publisher(mode)
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                self.assertEqual(state['tags'],{'v0.1.3':'a'*40})

    def test_uppercase_commit_is_sent_in_lowercase(self):
        result,state = self.run_publisher('success', commit='A'*40)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(state['tags'],{'v0.1.3':'a'*40})

    def test_draft_targeting_another_commit_is_not_published(self):
        result,state = self.run_publisher('changed-target')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('different commit',result.stderr)
        self.assertTrue(state.get('created'))
        self.assertFalse(state.get('published'))

    def test_published_tag_at_another_commit_fails_confirmation(self):
        result,state = self.run_publisher('wrong-tag-commit')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('does not point to the release commit',result.stderr)

    def test_existing_draft_beyond_the_first_page_prevents_upload(self):
        result,state = self.run_publisher('page-2-draft')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('already exists',result.stderr)
        self.assertEqual(state['pages'],[1,2])
        self.assertFalse(state.get('created'))

    def test_failed_later_release_page_prevents_upload(self):
        result,state = self.run_publisher('page-2-fail')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Could not list releases',result.stderr)
        self.assertFalse(state.get('created'))

    def test_two_drafts_for_the_tag_are_not_published(self):
        result,state = self.run_publisher('ambiguous-draft')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Could not identify the uploaded draft',result.stderr)
        self.assertFalse(state.get('published'))

    def test_automatic_ci_prevents_any_upload(self):
        result,state = self.run_publisher('automatic')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('manual-only',result.stderr)
        self.assertFalse(state.get('created'))

    def test_a_repository_without_workflows_can_publish(self):
        result,state = self.run_publisher('no-workflows')
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertTrue(state.get('published'))

    def test_an_unreadable_workflow_listing_prevents_upload(self):
        result,state = self.run_publisher('workflows-error')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Actions trigger settings',result.stderr)
        self.assertFalse(state.get('created'))

    def test_any_workflow_with_automatic_triggers_prevents_upload(self):
        result,state = self.run_publisher('extra-workflow')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('other.yml',result.stderr)
        self.assertFalse(state.get('created'))

    def test_commit_not_on_master_prevents_upload(self):
        result,state = self.run_publisher('unmerged')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('not on master',result.stderr)
        self.assertFalse(state.get('created'))

    def test_stderr_notices_do_not_corrupt_json(self):
        result,state = self.run_publisher('stderr-notice')
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertTrue(state.get('published'))

    def test_existing_draft_for_the_tag_prevents_upload(self):
        # GitHub never returns drafts by tag, so a leftover draft must be found in the release list.
        result,state = self.run_publisher('existing-draft')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('already exists',result.stderr)
        self.assertFalse(state.get('created'))

    def test_wrong_upload_digest_leaves_a_draft(self):
        result,state = self.run_publisher('bad-digest')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('checksum',result.stderr)
        self.assertTrue(state.get('created'))
        self.assertFalse(state.get('published'))
