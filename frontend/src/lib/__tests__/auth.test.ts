import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';

import { getAuthToken, setAuthToken } from '@/lib/api';
import {
  IDENTITY_KEY,
  availableModes,
  clearSession,
  firebaseConfig,
  type Identity,
  readSession,
  storeSession,
} from '@/lib/auth';

/**
 * The credential boundary.
 *
 * Two properties matter more than the rest. The development bypass must be
 * impossible to reach outside the local environment - a bypass that works in
 * staging is not a bypass, it is a published password. And a browser that
 * refuses storage must degrade to a shorter session rather than an unusable
 * console, because an operator in private mode is still an operator.
 */

const IDENTITY: Identity = {
  uid: 'dev-user',
  email: 'dev@localhost',
  displayName: 'Local Developer',
  photoURL: null,
  mode: 'firebase',
};

/** A complete Firebase web config; individual keys are removed per test. */
function stubFirebaseEnv(): void {
  vi.stubEnv('NEXT_PUBLIC_FIREBASE_API_KEY', 'key-abc');
  vi.stubEnv('NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN', 'aegis.firebaseapp.com');
  vi.stubEnv('NEXT_PUBLIC_FIREBASE_PROJECT_ID', 'aegis-prod');
}

function clearFirebaseEnv(): void {
  vi.stubEnv('NEXT_PUBLIC_FIREBASE_API_KEY', undefined);
  vi.stubEnv('NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN', undefined);
  vi.stubEnv('NEXT_PUBLIC_FIREBASE_PROJECT_ID', undefined);
}

beforeEach(() => {
  setAuthToken(null);
  window.localStorage.clear();
  clearFirebaseEnv();
});

afterEach(() => {
  setAuthToken(null);
});

describe('what this deployment can offer', () => {
  test('neither Firebase nor a local environment reports unconfigured, never an empty list', () => {
    // An empty array would let a caller render a sign-in page with no methods
    // on it and no explanation - the sign-in equivalent of collapsing "nothing
    // found" into "could not look".
    vi.stubEnv('NEXT_PUBLIC_AEGIS_ENV', 'production');

    expect(availableModes()).toEqual(['unconfigured']);
  });

  test('the local environment does not publish a development-token bypass', () => {
    vi.stubEnv('NEXT_PUBLIC_AEGIS_ENV', 'local');

    expect(availableModes()).toEqual(['unconfigured']);
  });

  test('an unconfigured environment does not offer the development bypass', () => {
    // The failure this guards is a deployment nobody configured: push the
    // console to a hosting platform, set no environment, and the bypass form
    // appears on a public URL. Absent configuration must grant nothing, so the
    // bypass is reachable only when something explicitly says `local`.
    vi.stubEnv('NEXT_PUBLIC_AEGIS_ENV', undefined);

    expect(availableModes()).toEqual(['unconfigured']);
  });

  test('a complete Firebase config offers Firebase even in a hardened environment', () => {
    vi.stubEnv('NEXT_PUBLIC_AEGIS_ENV', 'production');
    stubFirebaseEnv();

    expect(availableModes()).toEqual(['firebase']);
  });
});

describe('firebaseConfig', () => {
  test('a config missing any of apiKey, authDomain or projectId is no config at all', () => {
    // A partially-populated config initialises an app that fails later, at
    // sign-in, with an opaque SDK error. Returning null makes the deployment
    // mistake visible where it happens.
    for (const missing of [
      'NEXT_PUBLIC_FIREBASE_API_KEY',
      'NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN',
      'NEXT_PUBLIC_FIREBASE_PROJECT_ID',
    ]) {
      stubFirebaseEnv();
      vi.stubEnv(missing, undefined);
      expect(firebaseConfig(), `${missing} absent`).toBeNull();
    }
  });

  test('all three present yields a config', () => {
    stubFirebaseEnv();

    expect(firebaseConfig()).toMatchObject({
      apiKey: 'key-abc',
      authDomain: 'aegis.firebaseapp.com',
      projectId: 'aegis-prod',
    });
  });
});

describe('session persistence', () => {
  test('a stored session keeps the bearer token in memory only', () => {
    storeSession('token-123', IDENTITY);

    const restored = readSession();

    expect(restored.token).toBe('token-123');
    expect(restored.identity).toEqual(IDENTITY);
    expect(getAuthToken()).toBe('token-123');
    expect(window.localStorage.getItem('aegis_token')).toBeNull();
  });

  test('a reload waits for Firebase to restore the credential', () => {
    storeSession('token-123', IDENTITY);
    setAuthToken(null);

    expect(readSession()).toEqual({ token: null, identity: IDENTITY });
  });

  test('a browser that refuses storage keeps the in-memory session', () => {
    // Private mode throws from setItem. The operator loses persistence across
    // reloads, which is a nuisance; losing the session outright would be a
    // lockout.
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Access is denied for this document.', 'SecurityError');
    });

    expect(() => {
      storeSession('token-ephemeral', IDENTITY);
    }).not.toThrow();
    expect(getAuthToken()).toBe('token-ephemeral');
  });

  test('a browser that refuses reads yields an empty session rather than an exception', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('Access is denied for this document.', 'SecurityError');
    });

    expect(readSession()).toEqual({ token: null, identity: null });
  });

  test('a corrupted identity does not strand the console on a parse error', () => {
    // Half-written storage is a real failure mode. It must degrade to "signed
    // out", not to an unhandled SyntaxError during hydration.
    storeSession('token-123', IDENTITY);
    window.localStorage.setItem(IDENTITY_KEY, '{not json');

    expect(readSession()).toEqual({ token: null, identity: null });
  });

  test('clearing a session removes memory state and any legacy stored token', () => {
    storeSession('token-123', IDENTITY);
    window.localStorage.setItem('aegis_token', 'legacy-token');

    clearSession();

    expect(getAuthToken()).toBeNull();
    expect(window.localStorage.getItem('aegis_token')).toBeNull();
    expect(window.localStorage.getItem(IDENTITY_KEY)).toBeNull();
  });
});
