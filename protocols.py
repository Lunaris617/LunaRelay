"""Wire adapters for the supported inference endpoints."""
import copy
import json
from urllib.parse import quote


def endpoint(protocol, model, stream=False):
    if protocol == 'gemini':
        action = 'streamGenerateContent?alt=sse' if stream else 'generateContent'
        return f'/v1beta/models/{quote(model, safe="")}:{action}'
    return {'chat': '/v1/chat/completions', 'responses': '/v1/responses', 'messages': '/v1/messages'}[protocol]


def headers(protocol, key):
    if protocol == 'messages':
        return {'x-api-key': key, 'anthropic-version': '2023-06-01'}
    if protocol == 'gemini':
        return {'x-goog-api-key': key}
    return {'Authorization': f'Bearer {key}'}


def text_content(content):
    if isinstance(content, str):
        return content
    fragments = []
    for part in content or []:
        if part.get('type', 'text') not in ('text', 'input_text', 'output_text'):
            raise ValueError('跨协议路由目前仅转换文本内容')
        fragments.append(part.get('text', ''))
    return ''.join(fragments)


def normalize(protocol, data):
    if any(data.get(k) for k in ('previous_response_id', 'thinking', 'background', 'audio')):
        raise ValueError('此功能需要同协议上游')
    limit = data.get('max_completion_tokens') or data.get('max_tokens') or data.get('max_output_tokens') or 1024
    system = data.get('instructions') or data.get('system') or ''
    tools = data.get('tools') or []
    if protocol == 'gemini':
        limit = data.get('generationConfig', {}).get('maxOutputTokens', 1024)
        system = ''.join(p.get('text', '') for p in data.get('systemInstruction', {}).get('parts', []))
        messages = []
        for item in data.get('contents', []):
            if any('text' not in part for part in item.get('parts', [])):
                raise ValueError('此 Gemini 内容需要同协议上游')
            messages.append({'role': 'assistant' if item.get('role') == 'model' else 'user',
                             'content': ''.join(p['text'] for p in item['parts'])})
        if tools:
            tools = [{'name': f['name'], 'description': f.get('description', ''),
                      'parameters': f.get('parameters', {})} for group in tools for f in group.get('functionDeclarations', [])]
    else:
        messages = data.get('input', []) if protocol == 'responses' else data.get('messages', [])
        if isinstance(messages, str):
            messages = [{'role': 'user', 'content': messages}]
        tools = [tool.get('function', tool) for tool in tools]
    normalized = []
    for item in messages:
        role = item.get('role')
        if role in ('system', 'developer'):
            system = '\n'.join(filter(None, [text_content(system), text_content(item.get('content'))]))
        elif role in ('user', 'assistant'):
            normalized.append({'role': role, 'content': text_content(item.get('content'))})
        else:
            raise ValueError('此消息需要同协议上游')
    functions = []
    for tool in tools:
        if not tool.get('name'):
            raise ValueError('此工具需要同协议上游')
        functions.append({'name': tool['name'], 'description': tool.get('description', ''),
                          'parameters': tool.get('parameters') or tool.get('input_schema') or {'type': 'object'}})
    return normalized, text_content(system), limit, functions


def convert(source, target, data, model, stream):
    if source == target:
        output = copy.deepcopy(data)
        if target != 'gemini':
            output['model'] = model
        if target == 'chat' and stream:
            output.setdefault('stream_options', {})['include_usage'] = True
        return output
    messages, system, limit, tools = normalize(source, data)
    if source != target and tools:
        # Tool results, IDs, and streaming argument deltas require native semantics.
        raise ValueError('工具调用请选择与客户端协议匹配的上游')
    if target == 'gemini':
        result = {'contents': [{'role': 'model' if m['role'] == 'assistant' else 'user',
                               'parts': [{'text': m['content']}]} for m in messages],
                  'generationConfig': {'maxOutputTokens': limit}}
        if system:
            result['systemInstruction'] = {'parts': [{'text': system}]}
        return result
    result = {'model': model, 'stream': stream}
    if target == 'responses':
        result.update(input=messages, max_output_tokens=limit)
        if system:
            result['instructions'] = system
    elif target == 'messages':
        result.update(messages=messages, max_tokens=limit)
        if system:
            result['system'] = system
    else:
        result.update(messages=([{'role': 'system', 'content': system}] if system else []) + messages,
                      max_tokens=limit)
        if stream:
            result['stream_options'] = {'include_usage': True}
    return result


def usage(data):
    obj = data.get('response') or data.get('message') or data
    counts = obj.get('usage') or obj.get('usageMetadata') or {}
    if not isinstance(counts, dict):
        return 0, 0
    def count(key):
        value = counts.get(key)
        return min(max(value, 0), 10**9) if type(value) is int else 0
    prompt = max(count('input_tokens'), count('prompt_tokens'), count('promptTokenCount'))
    if 'input_tokens' in counts or 'cache_read_input_tokens' in counts or 'cache_creation_input_tokens' in counts:
        prompt += count('cache_read_input_tokens') + count('cache_creation_input_tokens')
    answer = max(count('output_tokens'), count('completion_tokens'),
                 count('candidatesTokenCount') + count('thoughtsTokenCount'))
    return prompt, max(answer, count('total_tokens') - prompt, count('totalTokenCount') - prompt)


def output_limit(protocol, data):
    if protocol == 'gemini':
        config = data.get('generationConfig', {})
        if not isinstance(config, dict):
            raise ValueError('generationConfig 必须是对象')
        return config.get('maxOutputTokens')
    if protocol == 'chat' and 'max_completion_tokens' in data:
        return data['max_completion_tokens']
    return data.get('max_output_tokens' if protocol == 'responses' else 'max_tokens')


def cap_output(protocol, data, limit):
    if protocol == 'gemini':
        data.setdefault('generationConfig', {})['maxOutputTokens'] = limit
    elif protocol == 'chat':
        field = 'max_completion_tokens' if 'max_completion_tokens' in data else 'max_tokens'
        data.pop('max_completion_tokens' if field == 'max_tokens' else 'max_tokens', None)
        data[field] = limit
    else:
        data['max_output_tokens' if protocol == 'responses' else 'max_tokens'] = limit


def require_bounded_text(data, protocol):
    config = data.get('generationConfig') or {}
    if data.get('n', 1) != 1 or config.get('candidateCount', 1) != 1:
        raise ValueError('Token 配额只支持每次生成一个候选结果')
    if any(data.get(field) for field in ('previous_response_id', 'conversation', 'background', 'audio', 'cachedContent')):
        raise ValueError('Token 配额需要在当前请求内提供完整文本上下文')
    tools = data.get('tools') or []
    if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
        raise ValueError('tools 必须是对象数组')
    if any((set(tool) != {'functionDeclarations'} if protocol == 'gemini'
            else tool.get('type', 'function') != 'function') for tool in tools):
        raise ValueError('Token 配额仅支持客户端函数工具')
    stack = [data]
    blocked_types = {'input_image', 'image_url', 'image', 'document', 'input_file', 'file', 'audio', 'input_audio'}
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            kind = item.get('type')
            has_media = bool({'inlineData', 'inline_data', 'fileData', 'file_data'} & item.keys())
            if has_media or (isinstance(kind, str) and kind in blocked_types):
                raise ValueError('Token 配额目前支持文本与函数工具，不接受媒体或文件输入')
            stack.extend(item.values())


def output_text(protocol, obj):
    if protocol == 'chat':
        choices = obj.get('choices') or []
        if choices:
            return (choices[0].get('delta') or choices[0].get('message') or {}).get('content') or ''
    if protocol == 'messages':
        if obj.get('type') == 'content_block_delta':
            return obj.get('delta', {}).get('text', '')
        return ''.join(p.get('text', '') for p in obj.get('content', []))
    if protocol == 'responses':
        if obj.get('type') == 'response.output_text.delta':
            return obj.get('delta', '')
        return ''.join(p.get('text', '') for o in obj.get('output', []) for p in o.get('content', []))
    if protocol == 'gemini':
        candidates = obj.get('candidates') or []
        if candidates:
            return ''.join(p.get('text', '') for p in candidates[0].get('content', {}).get('parts', []))
    return ''


def result(protocol, request_id, model, text, counts):
    i, o = counts
    if protocol == 'chat':
        return {'id': request_id, 'object': 'chat.completion', 'model': model, 'choices': [
            {'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': i, 'completion_tokens': o, 'total_tokens': i + o}}
    if protocol == 'messages':
        return {'id': request_id, 'type': 'message', 'role': 'assistant', 'model': model,
                'content': [{'type': 'text', 'text': text}], 'stop_reason': 'end_turn',
                'usage': {'input_tokens': i, 'output_tokens': o}}
    if protocol == 'responses':
        return {'id': request_id, 'object': 'response', 'status': 'completed', 'model': model,
                'output': [{'id': 'msg_' + request_id, 'type': 'message', 'role': 'assistant',
                            'status': 'completed', 'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}],
                'usage': {'input_tokens': i, 'output_tokens': o, 'total_tokens': i + o}}
    return {'candidates': [{'content': {'role': 'model', 'parts': [{'text': text}]}, 'finishReason': 'STOP'}],
            'usageMetadata': {'promptTokenCount': i, 'candidatesTokenCount': o, 'totalTokenCount': i + o}}


def sse(data, event=None):
    body = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    return (('event: ' + event + '\n') if event else '') + 'data: ' + body + '\n\n'


def tagged(event, **fields):
    return sse({'type': event, **fields}, event)


def begin_stream(protocol, request_id, model):
    if protocol == 'messages':
        message = result(protocol, request_id, model, '', (0, 0))
        message.update(content=[], stop_reason=None)
        yield tagged('message_start', message=message)
        yield tagged('content_block_start', index=0, content_block={'type': 'text', 'text': ''})
    elif protocol == 'responses':
        response = result(protocol, request_id, model, '', (0, 0))
        item = response['output'][0]
        content = item['content'][0]
        item = dict(item, status='in_progress', content=[])
        response.update(status='in_progress', output=[], usage=None)
        yield tagged('response.created', response=response)
        yield tagged('response.output_item.added', output_index=0, item=item)
        yield tagged('response.content_part.added', item_id=item['id'], output_index=0, content_index=0, part=content)


def delta_stream(protocol, request_id, model, delta):
    if protocol == 'messages':
        return tagged('content_block_delta', index=0, delta={'type': 'text_delta', 'text': delta})
    if protocol == 'responses':
        return tagged('response.output_text.delta', item_id='msg_' + request_id, output_index=0, content_index=0, delta=delta)
    if protocol == 'gemini':
        return sse({'candidates': [{'content': {'role': 'model', 'parts': [{'text': delta}]}}]})
    return sse({'id': request_id, 'object': 'chat.completion.chunk', 'model': model,
                'choices': [{'index': 0, 'delta': {'content': delta}, 'finish_reason': None}]})


def end_stream(protocol, request_id, model, text, counts):
    response = result(protocol, request_id, model, text, counts)
    if protocol == 'messages':
        yield tagged('content_block_stop', index=0)
        yield tagged('message_delta', delta={'stop_reason': 'end_turn', 'stop_sequence': None},
                     usage=response['usage'])
        yield tagged('message_stop')
    elif protocol == 'responses':
        item = response['output'][0]
        yield tagged('response.output_text.done', item_id=item['id'], output_index=0, content_index=0, text=text)
        yield tagged('response.content_part.done', item_id=item['id'], output_index=0, content_index=0, part=item['content'][0])
        yield tagged('response.output_item.done', output_index=0, item=item)
        yield tagged('response.completed', response=response)
    elif protocol == 'gemini':
        yield sse({'candidates': [{'finishReason': 'STOP'}], 'usageMetadata': response['usageMetadata']})
    else:
        yield sse({'id': request_id, 'object': 'chat.completion.chunk', 'model': model, 'usage': response['usage'],
                   'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
        yield sse('[DONE]')
