// Regression coverage for the local adaptation of WhiskeySockets/Baileys PR #2765.
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { test } from 'node:test';
import { pathToFileURL } from 'node:url';

const home = resolve(process.env.EVOLUTION_HOME || '../evolution-api');
const {
    buildPairingQRData, handleCompanionRegRefresh, makePairingQRRenderer,
} = await import(pathToFileURL(`${home}/node_modules/baileys/lib/Utils/companion-reg-client-utils.js`));
const logger = { warn() {}, debug() {}, info() {} };

function notification(childTag) {
    return {
        tag: 'notification', attrs: { type: 'companion_reg_refresh', id: 'refresh-1' },
        content: childTag ? [{ tag: childTag, attrs: {} }] : [],
    };
}

for (const child of ['companion_reg_refresh', 'pair-device-rotate-qr']) {
    test(`refresh with ${child} rotates and persists the secret without spending a ref`, () => {
        const creds = { advSecretKey: Buffer.alloc(32, 1).toString('base64') };
        const updates = [];
        const qrs = [];
        const renderer = makePairingQRRenderer(['ref-1', 'ref-2'], ref => {
            qrs.push(buildPairingQRData(ref, 'noise', 'identity', creds.advSecretKey, ['macOS', 'Chrome']));
        });
        assert.equal(renderer.next(), true);
        const previous = creds.advSecretKey;
        const result = handleCompanionRegRefresh(notification(child), {
            creds, logger, emitCredsUpdate: update => updates.push(update),
            refreshQR: () => {
                assert.deepEqual(updates, [{ advSecretKey: creds.advSecretKey }]);
                renderer.refresh();
            },
        });
        assert.equal(result, 'rotated');
        assert.notEqual(creds.advSecretKey, previous);
        assert.equal(Buffer.from(creds.advSecretKey, 'base64').length, 32);
        assert.equal(qrs.length, 2);
        const fields = qrs.map(qr => qr.split('#')[1].split(','));
        assert.equal(fields[0][0], 'ref-1');
        assert.equal(fields[1][0], 'ref-1');
        assert.equal(fields[1][3], creds.advSecretKey);
        renderer.next();
        assert.equal(qrs[2].split('#')[1].split(',')[0], 'ref-2');
    });
}

test('refreshing repeatedly does not exhaust the allocated refs', () => {
    const rendered = [];
    const renderer = makePairingQRRenderer(['first', 'second'], ref => rendered.push(ref));
    assert.equal(renderer.refresh(), false);
    assert.equal(renderer.next(), true);
    for (let count = 0; count < 100; count += 1) {
        assert.equal(renderer.refresh(), true);
    }
    assert.deepEqual(new Set(rendered), new Set(['first']));
    assert.equal(renderer.next(), true);
    assert.equal(rendered.at(-1), 'second');
    assert.equal(renderer.next(), false);
});

for (const child of [undefined, 'unexpected-child']) {
    test(`malformed refresh ${child} does not change authentication state`, () => {
        const creds = { advSecretKey: 'unchanged' };
        assert.equal(handleCompanionRegRefresh(notification(child), {
            creds, logger,
            emitCredsUpdate() { assert.fail('Unexpected credentials update'); },
            refreshQR() { assert.fail('Unexpected QR refresh'); },
        }), 'ignored_malformed');
        assert.equal(creds.advSecretKey, 'unchanged');
    });
}

test('a registered or phone-code session keeps its existing secret', () => {
    const creds = { advSecretKey: 'unchanged', me: { id: 'device' } };
    assert.equal(handleCompanionRegRefresh(notification('companion_reg_refresh'), {
        creds, logger,
        emitCredsUpdate() { assert.fail('Unexpected credentials update'); },
        refreshQR() { assert.fail('Unexpected QR refresh'); },
    }), 'ignored_registered');
    assert.equal(creds.advSecretKey, 'unchanged');
});
