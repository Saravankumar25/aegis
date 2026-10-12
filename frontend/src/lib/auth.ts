import { getAuthToken, setAuthToken } from './api';

/**
 * Authentication for the operator console.
 *
 * Two modes exist and they are deliberately not interchangeable:
 *
 * - **Firebase** is the real path. The browser signs in, receives an ID token,
 *   and the API verifies it against the project. This is the only mode allowed
 *   in staging or production.
 * - **Local development** uses the pre-shared bypass token the API accepts when
 *   `AUTH_DEV_MODE` is on. `Settings._production_hardening` refuses to boot a
 *   production API with that flag set, so this cannot silently become the
 *   production login.
 *
 * Firebase owns durable session recovery. ID tokens live only in memory and are
 * repopulated by the Firebase observer after reload; they are never copied into
 * script-readable browser storage.
 */

export const IDENTITY_KEY = 'aegis_identity';

export type AuthMode = 'firebase' | 'unconfigured';

export interface Identity {
  uid: string;
  email: string | null;
  displayName: string | null;
  photoURL: string | null;
  mode: AuthMode;
}

export interface FirebaseWebConfig {
  apiKey: string;
  authDomain: string;
  projectId: string;
  storageBucket?: string;
  messagingSenderId?: string;
  appId?: string;
}

/**
 * Read the public Firebase config.
 *
 * `NEXT_PUBLIC_*` values are inlined at build time, so these must be referenced
 * as full literal property accesses - destructuring `process.env` or indexing it
 * dynamically defeats the substitution and yields undefined in the browser.
 */
export function firebaseConfig(): FirebaseWebConfig | null {
  const apiKey = process.env.NEXT_PUBLIC_FIREBASE_API_KEY;
  const authDomain = process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN;
  const projectId = process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID;
  if (!apiKey || !authDomain || !projectId) return null;
  return {
    apiKey,
    authDomain,
    projectId,
    storageBucket: process.env.NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET,
    messagingSenderId: process.env.NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID,
    appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID,
  };
}

/**
 * Whether this build may offer the dev-token sign-in bypass.
 *
 * The bypass must be opted into explicitly. An unset `NEXT_PUBLIC_AEGIS_ENV`
 * previously defaulted to `local`, which meant the one deployment most likely
 * to be misconfigured - a fresh push to a hosting platform with no environment
 * set - was also the one that published a "paste your dev token" form on a
 * public URL. Fail closed instead: absent configuration grants nothing.
 */
/** Which sign-in methods this deployment can actually offer. */
export function availableModes(): AuthMode[] {
  return firebaseConfig() ? ['firebase'] : ['unconfigured'];
}

let emulatorConnected = false;

async function firebaseAuth() {
  const config = firebaseConfig();
  if (!config) throw new Error('Firebase is not configured for this deployment.');
  const [{ initializeApp, getApps, getApp }, auth] = await Promise.all([
    import('firebase/app'),
    import('firebase/auth'),
  ]);
  const app = getApps().length ? getApp() : initializeApp(config);
  const client = auth.getAuth(app);
  const emulator = process.env.NEXT_PUBLIC_FIREBASE_AUTH_EMULATOR_HOST;
  if (emulator && !emulatorConnected) {
    const origin = emulator.startsWith('http') ? emulator : `http://${emulator}`;
    auth.connectAuthEmulator(client, origin, { disableWarnings: true });
    emulatorConnected = true;
  }
  return { auth, client };
}

function identityFor(user: {
  uid: string;
  email: string | null;
  displayName: string | null;
  photoURL: string | null;
}): Identity {
  return {
    uid: user.uid,
    email: user.email,
    displayName: user.displayName,
    photoURL: user.photoURL,
    mode: 'firebase',
  };
}

export function storeSession(token: string, identity: Identity): void {
  setAuthToken(token);
  try {
    window.localStorage.setItem(IDENTITY_KEY, JSON.stringify(identity));
  } catch {
    // Identity is presentational only. The in-memory credential still works,
    // and Firebase remains the authority for recovery after reload.
  }
}

export function clearSession(): void {
  setAuthToken(null);
  try {
    // Remove the legacy key as a one-way migration from older builds that
    // persisted bearer tokens in script-readable storage.
    window.localStorage.removeItem('aegis_token');
    window.localStorage.removeItem(IDENTITY_KEY);
  } catch {
    /* non-fatal */
  }
}

export function readSession(): { token: string | null; identity: Identity | null } {
  if (typeof window === 'undefined') return { token: null, identity: null };
  try {
    const raw = window.localStorage.getItem(IDENTITY_KEY);
    const identity = raw ? (JSON.parse(raw) as Identity) : null;
    return { token: getAuthToken(), identity };
  } catch {
    return { token: null, identity: null };
  }
}

/**
 * Sign in with Firebase.
 *
 * The SDK is imported dynamically so a deployment with no Firebase config never
 * ships or parses it, and the landing page stays fast for the common case where
 * an operator is only reading the marketing content.
 */
export async function signInWithGoogle(): Promise<{ token: string; identity: Identity }> {
  const { auth, client } = await firebaseAuth();
  const provider = new auth.GoogleAuthProvider();
  const credential = await auth.signInWithPopup(client, provider);
  const token = await credential.user.getIdToken();
  const identity = identityFor(credential.user);
  storeSession(token, identity);
  return { token, identity };
}

export async function signInWithEmail(
  email: string,
  password: string,
): Promise<{ token: string; identity: Identity }> {
  const { auth, client } = await firebaseAuth();
  const credential = await auth.signInWithEmailAndPassword(client, email, password);
  const token = await credential.user.getIdToken();
  const identity = identityFor(credential.user);
  storeSession(token, identity);
  return { token, identity };
}

export async function signUpWithEmail(
  email: string,
  password: string,
): Promise<{ token: string; identity: Identity }> {
  const { auth, client } = await firebaseAuth();
  const credential = await auth.createUserWithEmailAndPassword(client, email, password);
  const token = await credential.user.getIdToken();
  const identity = identityFor(credential.user);
  storeSession(token, identity);
  return { token, identity };
}

export async function sendPasswordReset(email: string): Promise<void> {
  const { auth, client } = await firebaseAuth();
  await auth.sendPasswordResetEmail(client, email);
}

/** Restore Firebase's persisted user and always refresh the API token. */
export async function observeFirebaseSession(
  onSession: (session: { token: string; identity: Identity } | null) => void,
): Promise<() => void> {
  if (!firebaseConfig()) {
    clearSession();
    onSession(null);
    return () => {};
  }
  const { auth, client } = await firebaseAuth();
  return auth.onIdTokenChanged(client, (user) => {
    if (!user) {
      clearSession();
      onSession(null);
      return;
    }
    void user.getIdToken().then((token) => {
      const identity = identityFor(user);
      storeSession(token, identity);
      onSession({ token, identity });
    }).catch(() => {
      clearSession();
      onSession(null);
    });
  });
}

/** Sign out of Firebase too, so the next sign-in is a real one. */
export async function signOut(): Promise<void> {
  clearSession();
  try {
    const [{ getApps, getApp }, auth] = await Promise.all([
      import('firebase/app'),
      import('firebase/auth'),
    ]);
    if (!getApps().length) return;
    await auth.signOut(auth.getAuth(getApp()));
  } catch {
    // The local session is already gone, which is what protects this browser.
    // A failed remote sign-out is worth ignoring rather than blocking on.
  }
}
