"""Extraction quality checks use synthetic PDFs, never uploaded user documents."""
from io import BytesIO
import json
import socket

from PIL import Image
import pymupdf as fitz
import pytest

import pdf_engine
from pdf_engine import operate, PDFError


def column_pdf():
    with fitz.open() as doc:
        page = doc.new_page(width=595, height=842)
        page.insert_text((40,55), 'Quarterly research report', fontsize=22)
        for i in range(8):
            page.insert_text((40,100+i*22), f'Left column paragraph {i+1}.', fontsize=12)
            page.insert_text((320,100+i*22), f'Right column paragraph {i+1}.', fontsize=12)
        return doc.tobytes()


def table_pdf():
    with fitz.open() as doc:
        page = doc.new_page(width=595, height=842)
        page.insert_text((45,50), 'Price list', fontsize=20)
        for x in (45,215,385,550):
            page.draw_line((x,80),(x,230))
        for y in (80,130,180,230):
            page.draw_line((45,y),(550,y))
        for row,values in enumerate([['Item','Quantity','Price'],['Notebook','2','100'],['Pen','3','30']]):
            for column,value in enumerate(values):
                page.insert_text((55+column*170,110+row*50), value, fontsize=12)
        return doc.tobytes()


def mixed_pdf():
    with fitz.open() as doc:
        page = doc.new_page(width=300, height=400)
        page.insert_text((30,50), 'Selectable cafe text: café', fontsize=14)
        page = doc.new_page(width=300, height=400)
        image = BytesIO()
        Image.new('RGB',(300,400),'white').save(image,format='PNG')
        page.insert_image(page.rect,stream=image.getvalue())
        page = doc.new_page(width=300, height=400)
        page.insert_text((30,50),'Final selected page',fontsize=14)
        return doc.tobytes()


def extract(data, **options):
    return operate('extract-text',[('synthetic.pdf',data)],options)


def test_readable_layout_reads_one_column_before_the_next():
    result,_,_,summary = extract(column_pdf())
    text = result.decode()
    assert text.index('Left column paragraph 8.') < text.index('Right column paragraph 1.')
    assert text.index('Quarterly research report') < text.index('Left column paragraph 1.')
    assert summary['characters'] > 0


def test_markdown_preserves_table_headers_and_cells():
    result,name,mime,_ = extract(table_pdf(),format='md',text_layout='simple')
    text = result.decode()
    assert name.endswith('.md') and mime.startswith('text/markdown')
    assert '|Item|Quantity|Price|' in text
    assert '|Notebook|2|100|' in text and '|Pen|3|30|' in text


@pytest.mark.parametrize('layout',['simple','readable'])
def test_json_preserves_selected_page_order_and_unicode(layout):
    result,_,_,summary = extract(mixed_pdf(),format='json',pages='3,1',text_layout=layout)
    pages = json.loads(result)['pages']
    assert [item['page'] for item in pages] == [3,1]
    assert 'Final selected page' in pages[0]['text'] and 'café' in pages[1]['text']
    assert summary['empty_pages'] == []


def test_scanned_pages_report_an_actionable_error_instead_of_an_empty_download():
    with pytest.raises(PDFError,match='No selectable text'):
        extract(mixed_pdf(),pages='2')


def test_mixed_document_reports_the_pages_without_text(monkeypatch):
    def unexpected_ocr(*args,**kwargs):
        raise AssertionError('OCR must stay disabled')
    monkeypatch.setattr(fitz.Page,'get_textpage_ocr',unexpected_ocr)
    result,_,_,summary = extract(mixed_pdf(),format='json')
    assert json.loads(result)['pages'][1]['text'] == ''
    assert summary['empty_pages'] == [2]
    assert any('pages 2' in message for message in summary['warnings'])


def test_unresolved_layout_preserves_native_text_with_a_clear_warning(monkeypatch):
    import pymupdf4llm
    def unresolved(*args,**kwargs):
        raise ValueError('Synthetic unsupported layout')
    monkeypatch.setattr(pymupdf4llm,'to_text',unresolved)
    result,_,_,summary = extract(mixed_pdf(),pages='1')
    assert 'café' in result.decode()
    assert any('simpler formatting' in message for message in summary['warnings'])


def test_readable_extraction_requires_no_network(monkeypatch):
    def offline(*args,**kwargs):
        raise AssertionError('Text extraction must remain offline')
    monkeypatch.setattr(socket.socket,'connect',offline)
    result,_,_,_ = extract(table_pdf())
    assert 'Notebook' in result.decode()


def test_empty_layout_result_keeps_the_existing_text(monkeypatch):
    import pymupdf4llm
    monkeypatch.setattr(pymupdf4llm,'to_text',lambda *args,**kwargs: '')
    result,_,_,summary = extract(mixed_pdf(),pages='1')
    assert 'café' in result.decode()
    assert summary['empty_pages'] == []
    assert any('simpler formatting' in message for message in summary['warnings'])


def test_readable_extraction_handles_a_password_protected_pdf():
    protected,_,_,_ = operate('protect',[('sample.pdf',column_pdf())],{'user_password':'synthetic-reader'})
    result,_,_,_ = extract(protected,password='synthetic-reader')
    text = result.decode()
    assert text.index('Left column paragraph 8.') < text.index('Right column paragraph 1.')


def test_text_output_and_page_selection_limits(monkeypatch):
    monkeypatch.setattr(pdf_engine,'MAX_TEXT_PAGES',2)
    with pytest.raises(PDFError,match='pages at a time'):
        extract(mixed_pdf())
    monkeypatch.setattr(pdf_engine,'MAX_TEXT_BYTES',10)
    with pytest.raises(PDFError,match='extracted text exceeds'):
        extract(mixed_pdf(),pages='1',text_layout='simple')
