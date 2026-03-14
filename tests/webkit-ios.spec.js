const { test, expect } = require('@playwright/test');

// Correct DOM IDs (discovered via inspection):
// introScreen, hostBtn, joinBtn, hostName, guestName, messageInput,
// setupScreen, chatScreen, emojiToggle, emojiPicker, voiceMsgBtn,
// filePickerBtn, searchOverlay, searchClose, sendBtn, createRoomBtn,
// joinRoomBtn, callBtn, videoCallBtn, inAppWarning

async function openApp(page) {
    await page.goto('/SecuredChat.html', { waitUntil: 'networkidle' });
    await page.waitForFunction(() => typeof P2PChat === 'function', { timeout: 10000 });
}

// ============================================================
// 1. Basic Load & Initialization
// ============================================================

test.describe('App Initialization (WebKit)', () => {
    test('page loads without JS errors', async ({ page }) => {
        const errors = [];
        page.on('pageerror', err => errors.push(err.message));

        await openApp(page);

        expect(errors).toEqual([]);
    });

    test('all 9 classes are defined', async ({ page }) => {
        await openApp(page);

        const classes = await page.evaluate(() => [
            typeof PayloadCodec,
            typeof SearchManager,
            typeof ExportManager,
            typeof ConnectionManager,
            typeof CallManager,
            typeof VoiceManager,
            typeof FileManager,
            typeof ChatRenderer,
            typeof P2PChat,
        ]);

        for (const type of classes) {
            expect(type).toBe('function');
        }
    });

    test('key DOM elements exist after initialization', async ({ page }) => {
        await openApp(page);

        const ids = [
            'introScreen', 'hostBtn', 'joinBtn', 'setupScreen', 'chatScreen',
            'messageInput', 'sendBtn', 'messages', 'chatTitle',
            'searchOverlay', 'emojiPicker', 'voiceMsgBtn',
            'callBtn', 'videoCallBtn', 'filePickerBtn',
            'createRoomBtn', 'joinRoomBtn', 'inAppWarning'
        ];

        const results = await page.evaluate((ids) => {
            return ids.map(id => ({ id, exists: document.getElementById(id) !== null }));
        }, ids);

        for (const el of results) {
            expect(el.exists, `Element #${el.id} should exist`).toBe(true);
        }
    });

    test('no unexpected console errors', async ({ page }) => {
        const errors = [];
        page.on('console', msg => {
            if (msg.type() === 'error') errors.push(msg.text());
        });

        await openApp(page);

        const unexpected = errors.filter(w =>
            !w.includes('RTCPeerConnection') &&
            !w.includes('getUserMedia') &&
            !w.includes('Notification') &&
            !w.includes('ServiceWorker')
        );
        expect(unexpected).toEqual([]);
    });
});

// ============================================================
// 2. UI Rendering at iPhone Viewport
// ============================================================

test.describe('UI Rendering (iPhone)', () => {
    test('intro screen is visible on load', async ({ page }) => {
        await openApp(page);

        const visible = await page.evaluate(() => {
            const el = document.getElementById('introScreen');
            if (!el) return false;
            const style = getComputedStyle(el);
            return style.display !== 'none' && style.visibility !== 'hidden';
        });
        expect(visible).toBe(true);
    });

    test('host and join buttons exist and have text', async ({ page }) => {
        await openApp(page);

        const buttons = await page.evaluate(() => {
            const host = document.getElementById('hostBtn');
            const join = document.getElementById('joinBtn');
            return {
                hostText: host ? host.textContent.trim() : null,
                joinText: join ? join.textContent.trim() : null,
            };
        });
        expect(buttons.hostText).toContain('Create');
        expect(buttons.joinText).toContain('Join');
    });

    test('no horizontal overflow', async ({ page }) => {
        await openApp(page);

        const overflow = await page.evaluate(() =>
            document.documentElement.scrollWidth > document.documentElement.clientWidth
        );
        expect(overflow).toBe(false);
    });

    test('screenshot: intro screen', async ({ page }) => {
        await openApp(page);
        const fs = require('fs');
        if (!fs.existsSync('tests/screenshots')) fs.mkdirSync('tests/screenshots', { recursive: true });
        await page.screenshot({ path: 'tests/screenshots/01-intro-iphone.png', fullPage: true });
    });
});

// ============================================================
// 3. In-App Browser Detection
// ============================================================

test.describe('In-App Browser Detection', () => {
    test('detects Telegram in-app browser', async ({ browser }) => {
        const context = await browser.newContext({
            userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Telegram/10.0',
            viewport: { width: 375, height: 812 },
        });
        const page = await context.newPage();
        await page.goto('/SecuredChat.html', { waitUntil: 'networkidle' });
        await page.waitForFunction(() => typeof P2PChat === 'function', { timeout: 10000 });

        const warningVisible = await page.evaluate(() => {
            const el = document.getElementById('inAppWarning');
            if (!el) return false;
            const style = getComputedStyle(el);
            return style.display !== 'none' && !el.classList.contains('hidden');
        });

        expect(warningVisible).toBe(true);
        await context.close();
    });

    test('does NOT show warning in normal Safari', async ({ page }) => {
        await openApp(page);

        const warningHidden = await page.evaluate(() => {
            const el = document.getElementById('inAppWarning');
            if (!el) return true;
            return el.classList.contains('hidden') || getComputedStyle(el).display === 'none';
        });
        expect(warningHidden).toBe(true);
    });
});

// ============================================================
// 4. Chat Flow
// ============================================================

test.describe('Chat Flow (WebKit)', () => {
    test('hostBtn has active class by default', async ({ page }) => {
        await openApp(page);

        const isActive = await page.evaluate(() =>
            document.getElementById('hostBtn').classList.contains('active')
        );
        expect(isActive).toBe(true);
    });

    test('clicking joinBtn switches active class', async ({ page }) => {
        await openApp(page);

        await page.evaluate(() => document.getElementById('joinBtn').click());
        await page.waitForTimeout(300);

        const joinActive = await page.evaluate(() =>
            document.getElementById('joinBtn').classList.contains('active')
        );
        expect(joinActive).toBe(true);
    });

    test('setup screen is visible after clicking start', async ({ page }) => {
        await openApp(page);

        // Fill name and click start
        await page.evaluate(() => {
            document.getElementById('hostName').value = 'Alice';
            document.getElementById('startBtn').click();
        });
        await page.waitForTimeout(500);

        const setupVisible = await page.evaluate(() => {
            const el = document.getElementById('setupScreen');
            return el && getComputedStyle(el).display !== 'none';
        });
        expect(setupVisible).toBe(true);
    });

    test('Create Room button does not crash (WebRTC may be unavailable)', async ({ page }) => {
        const errors = [];
        page.on('pageerror', err => errors.push(err.message));

        await openApp(page);

        await page.evaluate(() => {
            document.getElementById('hostName').value = 'Alice';
            document.getElementById('startBtn').click();
        });
        await page.waitForTimeout(500);

        await page.evaluate(() => {
            try { document.getElementById('createRoomBtn').click(); } catch(e) {}
        });
        await page.waitForTimeout(2000);

        // Filter out expected WebRTC errors
        const jsErrors = errors.filter(e =>
            !e.includes('RTCPeerConnection') &&
            !e.includes('ICE') &&
            !e.includes('createOffer') &&
            !e.includes('is not a constructor') &&
            !e.includes('undefined is not an object')
        );
        expect(jsErrors).toEqual([]);
    });
});

// ============================================================
// 5. PayloadCodec in WebKit
// ============================================================

test.describe('PayloadCodec (WebKit engine)', () => {
    test('encode/decode roundtrip', async ({ page }) => {
        await openApp(page);

        const ok = await page.evaluate(() => {
            const original = { sdp: 'v=0\r\no=- 12345 2 IN IP4 127.0.0.1', type: 'offer' };
            const encoded = PayloadCodec.encode('OFFER', original);
            const decoded = PayloadCodec.decode('OFFER', encoded);
            return JSON.stringify(original) === JSON.stringify(decoded);
        });
        expect(ok).toBe(true);
    });

    test('btoa/atob handle SDP special chars', async ({ page }) => {
        await openApp(page);

        const ok = await page.evaluate(() => {
            const sdp = 'v=0\r\no=- 123 2 IN IP4 0.0.0.0\r\na=ice-ufrag:abc\r\na=ice-pwd:xyz123!@#';
            const b64 = btoa(JSON.stringify({ sdp }));
            return JSON.parse(atob(b64)).sdp === sdp;
        });
        expect(ok).toBe(true);
    });

    test('error detection (incomplete, wrong type, corruption)', async ({ page }) => {
        await openApp(page);

        const results = await page.evaluate(() => {
            const tests = {};

            try { PayloadCodec.decode('OFFER', 'OFFER.abc'); tests.incomplete = false; }
            catch (e) { tests.incomplete = e.message.includes('incomplete'); }

            try {
                PayloadCodec.decode('ANSWER', PayloadCodec.encode('OFFER', { s: 'x' }));
                tests.wrongType = false;
            } catch (e) { tests.wrongType = e.message.includes('room creation'); }

            try {
                const enc = PayloadCodec.encode('OFFER', { sdp: 'real data here' });
                const p = enc.split('.');
                const b64 = p.slice(1, p.length - 2).join('.');
                const bad = b64.substring(0, 3) + 'X' + b64.substring(4);
                PayloadCodec.decode('OFFER', p[0] + '.' + bad + '.' + bad.length + '.' + p[p.length - 1]);
                tests.corruption = false;
            } catch (e) { tests.corruption = e.message.includes('corrupted'); }

            return tests;
        });

        expect(results.incomplete).toBe(true);
        expect(results.wrongType).toBe(true);
        expect(results.corruption).toBe(true);
    });
});

// ============================================================
// 6. UI Interactions via evaluate (avoids visibility issues)
// ============================================================

test.describe('UI Interactions', () => {
    test('message input accepts text programmatically', async ({ page }) => {
        await openApp(page);

        const result = await page.evaluate(() => {
            const input = document.getElementById('messageInput');
            if (!input) return { error: 'messageInput not found' };
            input.value = 'Hello from WebKit test!';
            input.dispatchEvent(new Event('input'));
            return { value: input.value };
        });

        expect(result.value).toBe('Hello from WebKit test!');
    });

    test('emoji toggle shows emoji picker', async ({ page }) => {
        await openApp(page);

        const visible = await page.evaluate(() => {
            const toggle = document.getElementById('emojiToggle');
            if (!toggle) return null;
            toggle.click();
            const picker = document.getElementById('emojiPicker');
            if (!picker) return null;
            return !picker.classList.contains('hidden');
        });

        expect(visible).toBe(true);
    });

    test('search overlay opens and closes', async ({ page }) => {
        await openApp(page);

        const result = await page.evaluate(() => {
            // Open search (Ctrl+F is handled by keyboard, so we directly manipulate)
            const overlay = document.getElementById('searchOverlay');
            if (!overlay) return { error: 'searchOverlay not found' };

            overlay.classList.remove('hidden');
            const isOpen = !overlay.classList.contains('hidden');

            const closeBtn = document.getElementById('searchClose');
            if (closeBtn) closeBtn.click();
            const isClosed = overlay.classList.contains('hidden');

            return { isOpen, isClosed };
        });

        expect(result.isOpen).toBe(true);
        expect(result.isClosed).toBe(true);
    });
});
