import json
import pytest
from connectors.adapters.zykt import map_transcript
from connectors.adapters.base import AdapterError


def test_current_record_array_and_serialized_segments():
    segments = [{'BeginSec':60,'EndSec':62,'Text':'光学课堂'},
                {'BeginSec':62,'EndSec':65,'Text':'课件与板书','TransText':'Slides and board'}]
    result = map_transcript({'code':0,'list':[{'all_content':json.dumps(segments)}]})
    assert [item['text'] for item in result['segments']] == ['光学课堂','课件与板书']
    assert result['segments'][1]['translated_text'] == 'Slides and board'


@pytest.mark.parametrize('record',[{}, {'all_content':'not JSON'}, {'all_content':{}}])
def test_malformed_record_is_not_empty_success(record):
    with pytest.raises(AdapterError):
        map_transcript({'list':[record]})


def test_empty_record_list_is_valid_empty_transcript():
    assert map_transcript({'code':0,'list':[]})['segments'] == []
