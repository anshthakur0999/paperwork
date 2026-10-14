"""Reproduce text-extraction R&D with synthetic inputs only."""
from pathlib import Path
import importlib.util
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from pdf_engine import operate

spec = importlib.util.spec_from_file_location('synthetic_text_samples',ROOT/'tests/test_extract_text.py')
samples = importlib.util.module_from_spec(spec)
spec.loader.exec_module(samples)
target = ROOT/'output/extract-text-rd'
target.mkdir(parents=True,exist_ok=True)
measurements = []
for name,data,options in (
    ('columns-simple',samples.column_pdf(),{'text_layout':'simple'}),
    ('columns-readable',samples.column_pdf(),{'text_layout':'readable'}),
    ('table-markdown',samples.table_pdf(),{'format':'md'}),
    ('unicode-page-selection',samples.mixed_pdf(),{'format':'json','pages':'3,1'}),
    ('mixed-text-and-empty',samples.mixed_pdf(),{'format':'json'}),
):
    started = time.perf_counter()
    output,filename,mime,summary = operate('extract-text',[('sample.pdf',data)],options)
    elapsed = time.perf_counter()-started
    text = output.decode('utf-8')
    checks = {}
    if name.startswith('columns'):
        checks['left_column_completed_before_right_column'] = text.index('Left column paragraph 8.') < text.index('Right column paragraph 1.')
    if name == 'table-markdown':
        checks['table_headers_and_rows_preserved'] = all(row in text for row in ('|Item|Quantity|Price|','|Notebook|2|100|','|Pen|3|30|'))
    if name == 'unicode-page-selection':
        checks['page_order_and_unicode_preserved'] = [page['page'] for page in json.loads(text)['pages']] == [3,1] and 'café' in text
    if name == 'mixed-text-and-empty':
        checks['empty_page_identified'] = summary['empty_pages'] == [2]
    entry = {'sample':name,'seconds':round(elapsed,4),'bytes':len(output),'checks':checks,'summary':summary}
    measurements.append(entry)
    (target/(name+Path(filename).suffix)).write_bytes(output)
    (target/(name+'.pdf')).write_bytes(data)
    print(json.dumps(entry,ensure_ascii=False))
(target/'benchmark.json').write_text(json.dumps(measurements,ensure_ascii=False,indent=2),encoding='utf-8')
assert all(all(entry['checks'].values()) for entry in measurements if entry['sample'] != 'columns-simple')
