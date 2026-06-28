"""Track 3 Step 2: StreamBuffer / StreamResult 数据类单元测试

验证三态判定逻辑（complete / partial / failed）与 HC-1/HC-2/HC-3/HC-4 约束。
"""
import pytest
from iron.llm.backend import StreamBuffer, StreamResult


def test_stream_buffer_append_accumulates():
    """append 累积 chunk，flush 返回完整文本"""
    buf = StreamBuffer()
    buf.append("hello ")
    buf.append("world")
    assert buf.flush() == "hello world"
    assert buf.chunks_received == 2
    assert buf.is_partial() is True
    assert buf.is_empty() is False


def test_stream_buffer_empty_is_failed_state():
    """空 buffer 转 StreamResult 为 failed 状态（HC-3 允许 fallback）"""
    buf = StreamBuffer()
    assert buf.is_empty() is True
    assert buf.is_partial() is False
    result = StreamResult.from_buffer(buf)
    assert result.is_failed


def test_stream_buffer_mark_complete():
    """mark_complete 后转 StreamResult 为 complete 状态"""
    buf = StreamBuffer()
    buf.append("done")
    buf.mark_complete()
    assert buf.is_partial() is False
    result = StreamResult.from_buffer(buf)
    assert result.is_complete


def test_stream_buffer_flush_idempotent():
    """多次 flush 返回同一份内容，不重复拼接"""
    buf = StreamBuffer()
    buf.append("a")
    first = buf.flush()
    second = buf.flush()
    assert first == second == "a"


def test_stream_buffer_mark_failed_records_reason():
    """mark_failed 记录失败原因，partial 状态保留"""
    buf = StreamBuffer()
    buf.append("partial")
    buf.mark_failed("timeout")
    assert buf.failure_reason == "timeout"
    result = StreamResult.from_buffer(buf)
    assert result.is_partial
    assert result.error == "timeout"


def test_stream_result_from_buffer_partial_no_tool_calls():
    """HC-4: partial 状态不传 tool_calls（避免不完整 JSON 误触发工具调用）"""
    buf = StreamBuffer()
    buf.append("partial text")
    result = StreamResult.from_buffer(buf, tool_calls=[{"id": "x"}])
    assert result.is_partial
    assert result.tool_calls is None  # HC-4
    assert result.content == "partial text"


def test_stream_buffer_empty_chunk_ignored():
    """空字符串 chunk 不计入 chunks_received"""
    buf = StreamBuffer()
    buf.append("")
    assert buf.chunks_received == 0
    assert buf.is_empty()
