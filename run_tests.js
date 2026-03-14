// Node.js test runner for SecuredChat unit tests
// Run: node run_tests.js

global.btoa = (s) => Buffer.from(s).toString('base64');
global.atob = (s) => Buffer.from(s, 'base64').toString();

// === Code under test ===

function escapeHtml(text) {
    return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function formatDuration(seconds) {
    return Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
}

class PayloadCodec {
    static computeChecksum(str) {
        let sum = 0;
        for (let i = 0; i < str.length; i++) {
            sum = (sum + str.charCodeAt(i) * (i + 1)) & 0xFFFF;
        }
        return sum.toString(36);
    }

    static encode(type, jsonObj) {
        const json = JSON.stringify(jsonObj);
        const b64 = btoa(json);
        const checksum = this.computeChecksum(b64);
        return type + '.' + b64 + '.' + b64.length + '.' + checksum;
    }

    static decode(expectedType, raw) {
        const trimmed = raw.trim();
        const parts = trimmed.split('.');
        if (parts.length < 4) {
            throw new Error(
                'The code looks incomplete. It should start with "' + expectedType + '." \u2014 ' +
                'make sure you copied the entire code from start to end.'
            );
        }
        const type = parts[0];
        const checksum = parts[parts.length - 1];
        const lengthStr = parts[parts.length - 2];
        const b64 = parts.slice(1, parts.length - 2).join('.');

        if (type !== expectedType) {
            if (type === 'OFFER' && expectedType === 'ANSWER') {
                throw new Error('This is a room creation code, not a response code. You need to paste the code your friend sent BACK to you.');
            }
            if (type === 'ANSWER' && expectedType === 'OFFER') {
                throw new Error('This is a response code, not a room creation code. You need to paste the code from the person who CREATED the room.');
            }
            throw new Error('Unrecognized code format. Make sure you copied the right code.');
        }

        const expectedLength = parseInt(lengthStr, 10);
        if (b64.length !== expectedLength) {
            const diff = expectedLength - b64.length;
            throw new Error(
                'The code appears to be truncated (' + diff + ' characters missing). ' +
                'This often happens with SMS or apps that cut long messages. ' +
                'Try sending the code via email or a notes app instead.'
            );
        }

        const expectedChecksum = this.computeChecksum(b64);
        if (checksum !== expectedChecksum) {
            throw new Error('The code appears to be corrupted (some characters changed). Try copying it again.');
        }

        try {
            return JSON.parse(atob(b64));
        } catch (e) {
            throw new Error('The code could not be decoded. It may have been modified in transit.');
        }
    }
}

// === Test runner ===

let passed = 0, failed = 0, suite = '';

function describe(name, fn) { suite = name; fn(); }

function it(name, fn) {
    try {
        fn();
        passed++;
        console.log('  \x1b[32m\u2713\x1b[0m ' + suite + ' > ' + name);
    } catch (e) {
        failed++;
        console.log('  \x1b[31m\u2717\x1b[0m ' + suite + ' > ' + name);
        console.log('    \x1b[31m' + e.message + '\x1b[0m');
    }
}

function expect(val) {
    return {
        toBe(expected) {
            if (val !== expected) throw new Error('Expected ' + JSON.stringify(expected) + ', got ' + JSON.stringify(val));
        },
        toEqual(expected) {
            const a = JSON.stringify(val), b = JSON.stringify(expected);
            if (a !== b) throw new Error('Expected ' + b + ', got ' + a);
        },
        toThrow(substring) {
            if (typeof val !== 'function') throw new Error('Expected a function');
            let threw = false, msg = '';
            try { val(); } catch (e) { threw = true; msg = e.message; }
            if (!threw) throw new Error('Expected function to throw, but it did not');
            if (substring && !msg.includes(substring))
                throw new Error('Expected error containing "' + substring + '", got "' + msg + '"');
        },
        toMatch(regex) {
            if (!regex.test(val)) throw new Error('"' + val + '" does not match ' + regex);
        }
    };
}

// === Tests ===

describe('escapeHtml', () => {
    it('escapes ampersands', () => {
        expect(escapeHtml('a & b')).toBe('a &amp; b');
    });
    it('escapes angle brackets', () => {
        expect(escapeHtml('<b>hi</b>')).toBe('&lt;b&gt;hi&lt;/b&gt;');
    });
    it('returns empty string unchanged', () => {
        expect(escapeHtml('')).toBe('');
    });
    it('leaves safe text unchanged', () => {
        expect(escapeHtml('Hello world')).toBe('Hello world');
    });
    it('handles all three special chars', () => {
        expect(escapeHtml('a < b & b > c')).toBe('a &lt; b &amp; b &gt; c');
    });
});

describe('formatFileSize', () => {
    it('formats bytes', () => { expect(formatFileSize(500)).toBe('500 B'); });
    it('formats KB', () => { expect(formatFileSize(2048)).toBe('2.0 KB'); });
    it('formats MB', () => { expect(formatFileSize(5242880)).toBe('5.0 MB'); });
    it('handles zero', () => { expect(formatFileSize(0)).toBe('0 B'); });
    it('boundary at 1024', () => { expect(formatFileSize(1024)).toBe('1.0 KB'); });
    it('boundary at 1MB', () => { expect(formatFileSize(1048576)).toBe('1.0 MB'); });
});

describe('formatDuration', () => {
    it('zero seconds', () => { expect(formatDuration(0)).toBe('0:00'); });
    it('seconds only', () => { expect(formatDuration(45)).toBe('0:45'); });
    it('minutes and seconds', () => { expect(formatDuration(125)).toBe('2:05'); });
    it('pads single-digit seconds', () => { expect(formatDuration(61)).toBe('1:01'); });
    it('exact minutes', () => { expect(formatDuration(120)).toBe('2:00'); });
});

describe('PayloadCodec.computeChecksum', () => {
    it('returns base-36 string', () => {
        expect(PayloadCodec.computeChecksum('hello')).toMatch(/^[0-9a-z]+$/);
    });
    it('is deterministic', () => {
        expect(PayloadCodec.computeChecksum('test')).toBe(PayloadCodec.computeChecksum('test'));
    });
    it('differs for different inputs', () => {
        const a = PayloadCodec.computeChecksum('aaa');
        const b = PayloadCodec.computeChecksum('bbb');
        expect(a !== b).toBe(true);
    });
    it('empty string returns 0', () => {
        expect(PayloadCodec.computeChecksum('')).toBe('0');
    });
});

describe('PayloadCodec.encode', () => {
    it('produces TYPE.b64.length.checksum format', () => {
        const encoded = PayloadCodec.encode('OFFER', { sdp: 'test' });
        const parts = encoded.split('.');
        expect(parts[0]).toBe('OFFER');
        expect(parts.length >= 4).toBe(true);
    });
    it('base64 decodes back to original JSON', () => {
        const encoded = PayloadCodec.encode('OFFER', { foo: 'bar' });
        const parts = encoded.split('.');
        const b64 = parts.slice(1, parts.length - 2).join('.');
        const decoded = JSON.parse(atob(b64));
        expect(decoded).toEqual({ foo: 'bar' });
    });
    it('length field matches base64 length', () => {
        const encoded = PayloadCodec.encode('ANSWER', { x: 1 });
        const parts = encoded.split('.');
        const b64 = parts.slice(1, parts.length - 2).join('.');
        expect(parseInt(parts[parts.length - 2], 10)).toBe(b64.length);
    });
    it('checksum field is correct', () => {
        const encoded = PayloadCodec.encode('OFFER', { sdp: 'v=0...' });
        const parts = encoded.split('.');
        const b64 = parts.slice(1, parts.length - 2).join('.');
        expect(parts[parts.length - 1]).toBe(PayloadCodec.computeChecksum(b64));
    });
});

describe('PayloadCodec.decode', () => {
    it('roundtrips OFFER', () => {
        const original = { sdp: 'v=0\r\no=- 12345', type: 'offer' };
        const encoded = PayloadCodec.encode('OFFER', original);
        expect(PayloadCodec.decode('OFFER', encoded)).toEqual(original);
    });
    it('roundtrips ANSWER', () => {
        const original = { sdp: 'v=0\r\no=- 67890', type: 'answer' };
        const encoded = PayloadCodec.encode('ANSWER', original);
        expect(PayloadCodec.decode('ANSWER', encoded)).toEqual(original);
    });
    it('roundtrips complex nested objects', () => {
        const original = { sdp: 'test', candidates: [{ a: 1 }, { b: 2 }], nested: { deep: true } };
        const encoded = PayloadCodec.encode('OFFER', original);
        expect(PayloadCodec.decode('OFFER', encoded)).toEqual(original);
    });
    it('tolerates leading/trailing whitespace', () => {
        const encoded = PayloadCodec.encode('OFFER', { ok: true });
        expect(PayloadCodec.decode('OFFER', '  ' + encoded + '  \n')).toEqual({ ok: true });
    });
    it('throws on incomplete code (too few parts)', () => {
        expect(() => PayloadCodec.decode('OFFER', 'OFFER.abc')).toThrow('incomplete');
    });
    it('throws helpful message: OFFER given but ANSWER expected', () => {
        const encoded = PayloadCodec.encode('OFFER', { sdp: 'x' });
        expect(() => PayloadCodec.decode('ANSWER', encoded)).toThrow('room creation code');
    });
    it('throws helpful message: ANSWER given but OFFER expected', () => {
        const encoded = PayloadCodec.encode('ANSWER', { sdp: 'x' });
        expect(() => PayloadCodec.decode('OFFER', encoded)).toThrow('response code');
    });
    it('throws on unrecognized type', () => {
        const raw = 'BOGUS.' + btoa('{}') + '.4.abc';
        expect(() => PayloadCodec.decode('OFFER', raw)).toThrow('Unrecognized');
    });
    it('detects truncation', () => {
        const encoded = PayloadCodec.encode('OFFER', { sdp: 'some long sdp data here for testing' });
        const parts = encoded.split('.');
        parts[1] = parts[1].substring(0, parts[1].length - 5);
        expect(() => PayloadCodec.decode('OFFER', parts.join('.'))).toThrow('truncated');
    });
    it('detects corruption (modified characters)', () => {
        const encoded = PayloadCodec.encode('OFFER', { sdp: 'real sdp data here' });
        const parts = encoded.split('.');
        const b64 = parts.slice(1, parts.length - 2).join('.');
        const corrupted = b64.substring(0, 3) + 'X' + b64.substring(4);
        const bad = parts[0] + '.' + corrupted + '.' + corrupted.length + '.' + parts[parts.length - 1];
        expect(() => PayloadCodec.decode('OFFER', bad)).toThrow('corrupted');
    });
});

// === Summary ===
console.log('\n' + (failed === 0 ? '\x1b[32m' : '\x1b[31m') +
    passed + '/' + (passed + failed) + ' passed' +
    (failed ? ', ' + failed + ' FAILED' : ' \u2014 all green') +
    '\x1b[0m');

process.exit(failed > 0 ? 1 : 0);
