"""Layout-aware PDF document model backed only by pdf-inspector."""
from __future__ import annotations

import json
import re
from pathlib import Path
from statistics import median

import pdf_inspector


def clean(value: str | None) -> str:
    return re.sub(r'\s+', '', value or '')


class View:
    def __init__(self, nodes):
        self.nodes = list(nodes)
        self.text = ''
        self.positions = []
        for node in self.nodes:
            for offset, char in enumerate(node['text']):
                if not char.isspace():
                    self.text += char
                    self.positions.append((node, offset))

    def evidence(self, start, end):
        rows = []
        for node, offset in self.positions[start:end]:
            if rows and rows[-1]['item_id'] == node['item_id']:
                rows[-1]['end'] = offset + 1
            else:
                rows.append({
                    'item_id': node['item_id'], 'page': node['page'],
                    'start': offset, 'end': offset + 1,
                    'section': node.get('section', ''),
                    'bbox': [node['x'], node['y'], node['width'], node['height']],
                })
        by_id = {node['item_id']: node for node in self.nodes}
        for row in rows:
            row['text'] = by_id[row['item_id']]['text'][row['start']:row['end']]
        return rows

    def capture(self, start, end, label='', rule=''):
        evidence = self.evidence(start, end)
        return {
            'value': self.text[start:end],
            'page': sorted({e['page'] for e in evidence}),
            'evidence': evidence, 'label': label, 'rule': rule,
        }

    def find(self, pattern, label='', rule='', flags=0):
        return [self.capture(m.start('v'), m.end('v'), label, rule)
                for m in re.finditer(pattern, self.text, flags)]


class Document:
    def __init__(self, pdf: Path, output: Path, keep_intermediate=True):
        raw = pdf_inspector.extract_text_with_positions(str(pdf))
        page_count = pdf_inspector.detect_pdf(str(pdf)).page_count
        self.items = [{
            **{k: getattr(item, k) for k in ['page', 'text', 'x', 'y', 'width', 'height', 'font_size', 'item_type']},
            'item_id': f'p{item.page}_t{i}',
        } for i, item in enumerate(raw)]
        self.nodes = [n for n in self.items if not n['text'].startswith('[Image:')
                      and not (n['y'] < 70 and re.fullmatch(r'\d+|https?://\S+', clean(n['text'])))]
        self.pages = [{
            'pdf_page': page,
            'text': '\n'.join(n['text'] for n in self.nodes if n['page'] == page),
            'image_count': sum(n['text'].startswith('[Image:') for n in self.items if n['page'] == page),
        } for page in range(1, page_count + 1)]
        self.groups, self.section_notes = self.locate()
        for node in self.items:
            node['section'] = next((name for name, pages in self.groups.items() if node['page'] in pages), 'other')
        self.all = View(self.nodes)
        # Covers and title pages often contain the authoritative tender number,
        # but they are deliberately outside the announcement section.
        self.cover = View(n for n in self.nodes if n['page'] <= 3)
        self.announcement = View(n for n in self.nodes if n['page'] in self.groups['announcement'])
        self.front = View(n for n in self.nodes if n['page'] in self.groups['front'])
        self.general = View(n for n in self.nodes if n['page'] in self.groups['general'])
        self.evaluation = View(n for n in self.nodes if n['page'] in self.groups['evaluation'])
        content = []
        self.column_thresholds = {}
        for page in self.groups['front']:
            nodes = [n for n in self.nodes if n['page'] == page]
            xs = [n['x'] for n in nodes if len(clean(n['text'])) > 20]
            threshold = median(xs) - 25 if xs else 0
            self.column_thresholds[page] = threshold
            content.extend(n for n in nodes if n['x'] >= threshold)
        self.front_content = View(content or self.front.nodes)
        self.clauses = self._clauses()
        if keep_intermediate:
            output.mkdir(parents=True, exist_ok=True)
            self.write(output / 'pages.json', self.pages)
            self.write(output / 'sections.json', {'groups': self.groups, 'notes': self.section_notes})
            markdown = pdf_inspector.extract_pages_markdown(str(pdf))
            (output / 'document.md').write_text('\n\n'.join(
                f'<!-- PDF PAGE {page.page + 1} -->\n{page.markdown}'
                for page in markdown.pages
            ), encoding='utf-8')

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

    def locate(self):
        texts = {p['pdf_page']: clean(p['text']) for p in self.pages}
        notes = []
        announcement_candidates = [p for p, text in texts.items()
            if any(title in text for title in ['招标公告','投标邀请书','采购公告','谈判采购公告','单一来源采购公告'])
            and not re.search(r'\.\.{5,}', text)]
        semantic_candidates = [p for p, text in texts.items()
            if ('招标条件' in text and ('项目概况' in text or '招标范围' in text))
            or ('项目概况' in text and ('采购需求' in text or '采购范围' in text or '供应商资格' in text))]
        # Invitation-only documents may have no public announcement chapter.
        # Use the earliest valid invitation/announcement/semantic anchor.
        start = min(semantic_candidates + announcement_candidates or [1])
        if not semantic_candidates and not announcement_candidates:
            notes.append('未识别公告标题，公告候选区降级为文档前12页。')
        front_candidates = [p for p, text in texts.items() if p > start and
            ('投标人须知前附表' in text or '申请人须知前附表' in text or '供应商须知前附表' in text or
             ('1.1.2' in text and '1.3.1' in text and '资金来源' in text))]
        front_start = min(front_candidates) if front_candidates else None
        general_candidates = [p for p, text in texts.items() if front_start and p > front_start and
            ('投标人须知正文' in text or '申请人须知正文' in text or '供应商须知正文' in text or
             ('1.1.1' in text and '见投标人须知前附表' in text))]
        general_start = min(general_candidates) if general_candidates else None
        strong_evaluation = [p for p, text in texts.items() if p > (front_start or start) and
            (title := re.search(r'(?:第三|第四)章(?:入围)?(?:评标|评审)办法', text)) and title.start() < 180 and
            ('办法前附表' in text or '评审方法' in text or
             re.search(r'(?:评标|评审)办法[（(].+?法[）)]', text))]
        evaluation_candidates = strong_evaluation or [p for p, text in texts.items() if p > (front_start or start) and
            (('评标办法' in text or '评审办法' in text) and
             ('评标委员会' in text or '评审小组' in text or '评审标准' in text or '综合评估法' in text or '综合评分法' in text))]
        evaluation_start = min(evaluation_candidates) if evaluation_candidates else None
        contract_candidates = [p for p, text in texts.items() if evaluation_start and p > evaluation_start and
            ('合同条款及格式' in text or '通用合同条款' in text or '合同草案' in text or '合同文本' in text)]
        contract_start = min(contract_candidates) if contract_candidates else None
        ann_end = front_start or min(page_count := len(self.pages) + 1, start + 8)
        if not front_start:
            notes.append('未识别投标人须知前附表，表格字段仅使用全局明确标签。')
        front_end = general_start or evaluation_start or (min(len(self.pages) + 1, front_start + 12) if front_start else start)
        general_end = evaluation_start or contract_start or (
            min(len(self.pages) + 1, general_start + 20) if general_start else start)
        eval_end = contract_start or (min(len(self.pages) + 1, evaluation_start + 20) if evaluation_start else start)
        return {
            'announcement': list(range(start, ann_end)),
            'front': list(range(front_start, front_end)) if front_start else [],
            'general': list(range(general_start, general_end)) if general_start else [],
            'evaluation': list(range(evaluation_start, eval_end)) if evaluation_start else [],
        }, notes

    def _clauses(self):
        starts = []
        for index, node in enumerate(self.announcement.nodes):
            text = clean(node['text'])
            match = re.match(r'^(\d+(?:\.\d+)+)[、.]?', text)
            if match:
                # Locate this node's first character in the normalized view.
                start = next((i for i, (n, _) in enumerate(self.announcement.positions) if n is node), None)
                if start is not None:
                    starts.append((start, match.group(1), start + len(match.group(0))))
        clauses = []
        for i, (start, number, body) in enumerate(starts):
            end = starts[i + 1][0] if i + 1 < len(starts) else len(self.announcement.text)
            clauses.append({'number': number, 'start': body, 'end': end, 'text': self.announcement.text[body:end]})
        return clauses

    def clause_fields(self, labels, rule):
        results = []
        for clause in self.clauses:
            for label in labels:
                match = re.match(re.escape(label) + r'[：:]?(?P<v>.+)', clause['text'])
                if match:
                    results.append(self.announcement.capture(
                        clause['start'] + match.start('v'), clause['start'] + match.end('v'), label, rule))
        return results

    def row_values(self, label):
        results = []
        for node in self.nodes:
            if node['page'] not in self.groups['front'] or clean(node['text']) != label:
                continue
            right = [x for x in self.nodes if x['page'] == node['page']
                     and x['x'] > node['x'] + node['width'] + 2 and abs(x['y'] - node['y']) < 8]
            if right:
                view = View(sorted(right, key=lambda x: x['x']))
                results.append(view.capture(0, len(view.text), label, '同一表格行右侧单元格'))
        return results

    def row_band_values(self, label):
        """Read a wrapped value cell from a front-table row.

        Some tender documents vertically centre the row label while the value
        starts above it and continues below it.  Same-baseline matching loses
        those lines.  Clause-number anchors in the left column provide stable
        row boundaries without relying on document-specific wording.
        """
        results = []
        number = re.compile(r'^\d+(?:\.\d+)+$')
        for node in self.nodes:
            if node['page'] not in self.groups['front'] or clean(node['text']) != label:
                continue
            page_nodes = [n for n in self.nodes if n['page'] == node['page']]
            anchors = [n for n in page_nodes if n['x'] < node['x'] and number.fullmatch(clean(n['text']))]
            if not anchors:
                continue
            current = min(anchors, key=lambda n: abs(n['y'] - node['y']))
            above = sorted((n['y'] for n in anchors if n['y'] > current['y']))
            below = sorted((n['y'] for n in anchors if n['y'] < current['y']), reverse=True)
            upper = (current['y'] + above[0]) / 2 if above else current['y'] + 45
            lower = (current['y'] + below[0]) / 2 if below else current['y'] - 45
            left = node['x'] + node['width'] + 2
            values = [n for n in page_nodes if n['x'] > left and lower < n['y'] < upper]
            if values:
                view = View(sorted(values, key=lambda n: (-n['y'], n['x'])))
                results.append(view.capture(0, len(view.text), label, '表格行坐标边界内的多行值'))
        return results
