"""Portable synthetic fixtures for the supported new-UI source proof shape."""
import copy
import hashlib
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path[:0] = [str(SCRIPTS), str(SCRIPTS.parents[1] / '.shared')]
from bridge_store import BridgeError
from copied_link_source import build_source_link_proof, validate_source_link_proof, require_source_proof_status


class CopiedLinkSourceTests(unittest.TestCase):
    def setUp(self):
        user, assistant = 'synthetic-user', 'synthetic-answer'
        url, owner, page = 'https://chatgpt.com/c/synthetic-conversation', 'synthetic-owner', '7'
        group = {'type': 'grouped_webpages', 'safe_urls': ['https://example.com/reference'],
                 'alt': '[reference](https://example.com/reference)', 'matched_text': '',
                 'items': [{'supporting_websites': [{'url': 'https://example.com/reference'}]}]}
        refs = [group, {'type': 'sources_footnote', 'safe_urls': []}]
        content, expanded = [], []
        for i in range(30):
            name = f'file-{i}.txt'
            link = f'[file {i}](sandbox:/mnt/data/{name})'
            refs.append({'type': 'file', 'message_id': assistant, 'matched_text': link,
                         'link_markdown': link, 'name': name, 'file_name': name,
                         'sandbox_path': '/mnt/data/' + name})
            if i == 14:
                content.append(':chatgpt-content-reference{index="0"}')
                expanded.append(group['alt'])
            token = ':chatgpt-content-reference{index="' + str(i + 2) + '"}'
            content.append(token + link)
            expanded.append(token + link)
        turn = {'key': 'synthetic-turn', 'entry_id': 'synthetic-turn', 'entry_turn_key': user,
                'status': 'complete', 'items': [
                    {'type': 'user-message', 'message_id': user},
                    {'type': 'chatgpt-reasoning-group', 'completed': True},
                    {'type': 'assistant-message', 'message_id': assistant, 'completed': True}]}
        self.prompt = 'Synthetic prompt'
        self.source = {'schema': 'copied-link-source/v1', 'source_route': 'new-ui-fiber-item',
            'status': 'available', 'conversation_url': url, 'owner_token': owner, 'page_id': page,
            'complete': True, 'truncated': False, 'records': {'user': user, 'assistant': assistant},
            'user': {'id': user, 'messageId': user, 'type': 'user-message', 'message': self.prompt},
            'assistant': {'id': assistant, 'type': 'assistant-message', 'completed': True,
                'phase': 'final_answer', 'latest_message_id': assistant, 'source_message_ids': [assistant],
                'content': '\n'.join(content), 'content_reference_message_ids': [assistant] * 32,
                'content_reference_message_statuses': ['finished_successfully'] * 32,
                'content_references': refs}, 'turn': turn, 'user_turn': copy.deepcopy(turn),
            'dom': {'anchors': [{'href': group['safe_urls'][0]}],
                    'resource_rows': [{'filename': 'file-0.txt'}, {'filename': 'file-1.txt'}],
                    'sandbox_buttons': 0, 'structural_links': 3}}
        self.answer = '\n'.join(expanded)
        prompt_digest = hashlib.sha256(self.prompt.encode()).hexdigest()
        self.submission = {'comparison_codec': 'exact', 'conversation_url': url,
            'copy_provenance': 'visible-copy-write/v1', 'copy_sha256': prompt_digest,
            'prompt_sha256': prompt_digest, 'remote_turn_id': user, 'tab_owner_token': owner}
        self.identity = {'assistant_id': assistant, 'user_id': user, 'expected_url': url,
            'expected_owner': owner, 'expected_page_id': page, 'expected_prompt': self.prompt}

    def build(self, source=None, answer=None):
        source = self.source if source is None else source
        return build_source_link_proof(source, source, self.answer if answer is None else answer,
            **self.identity, structural_metrics={'links': 3}, copied_metrics={'links': 31},
            submission_copy=self.submission)

    def test_source_occurrences_prove_copied_links_without_structural_count_equality(self):
        proof = self.build()
        self.assertEqual(proof['source_link_count'], 31)
        self.assertEqual(proof['structural_link_count'], 3)
        validate_source_link_proof(proof, self.answer, **self.identity)

    def test_foreign_identity_pending_turn_and_changed_prompt_are_rejected(self):
        mutations = [('owner_token', 'other'), ('page_id', '8'), ('conversation_url', 'https://chatgpt.com/c/other')]
        for key, value in mutations:
            source = copy.deepcopy(self.source)
            source[key] = value
            with self.subTest(key=key), self.assertRaises(BridgeError): self.build(source)
        for branch, key, value in [('user', 'message', 'other prompt'), ('assistant', 'completed', False)]:
            source = copy.deepcopy(self.source)
            source[branch][key] = value
            with self.subTest(branch=branch), self.assertRaises(BridgeError): self.build(source)
        source = copy.deepcopy(self.source)
        source['turn']['items'][1]['completed'] = False
        with self.assertRaises(BridgeError): self.build(source)

    def test_dropped_reordered_or_retargeted_link_cannot_be_adopted(self):
        lines = self.answer.splitlines()
        for answer in ('\n'.join(lines[1:]), '\n'.join(reversed(lines)),
                       self.answer.replace('sandbox:/mnt/data/file-0.txt', 'sandbox:/mnt/data/other.txt', 1)):
            with self.subTest(answer=answer[:30]), self.assertRaises(BridgeError): self.build(answer=answer)
        proof = self.build()
        with self.assertRaises(BridgeError): validate_source_link_proof(proof, self.answer + '\nchanged', **self.identity)

    def test_available_source_requires_an_immutable_receipt(self):
        with self.assertRaises(BridgeError):
            require_source_proof_status(route='browser-fallback', answer_format='copied-markdown',
                                        status='available', proof_present=False)


if __name__ == '__main__':
    unittest.main()
