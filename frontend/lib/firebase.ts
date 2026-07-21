// Firebase client SDK — used for exactly one thing: proving a Google identity.
//
// The security design (ESD §8, CLAUDE.md §12) is that the browser's durable credential is
// Aegis's own httpOnly cookie, which JavaScript cannot read. The Firebase ID token is a
// short-lived bearer credential that would undermine that if it were persisted, because the
// SDK's default persistence is IndexedDB — readable by any script on the origin.
//
// Two measures keep it out of reach, in order of importance:
//   1. Persistence is set to in-memory BEFORE sign-in, so the token is never written to
//      IndexedDB or localStorage in the first place.
//   2. signOut() runs immediately after the exchange, so it does not even linger in memory
//      for the rest of the page's life.
//
// The config values below are public by design — they identify the project and ship in the
// bundle. They are not secrets; the real boundary is backend verification of the ID token.

import { initializeApp, getApps, type FirebaseApp } from "firebase/app";
import {
  GoogleAuthProvider,
  getAuth,
  inMemoryPersistence,
  setPersistence,
  signInWithPopup,
  signOut,
  type Auth,
} from "firebase/auth";

const config = {
  apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
  authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
  projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
  storageBucket: process.env.NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET,
  messagingSenderId: process.env.NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID,
  appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID,
};

export class FirebaseConfigMissing extends Error {
  constructor(missing: string[]) {
    super(
      `Firebase is not configured: missing ${missing.join(", ")}. ` +
        `Copy frontend/.env.example to frontend/.env.local and fill it in.`,
    );
  }
}

function getFirebaseApp(): FirebaseApp {
  const missing = Object.entries(config)
    .filter(([, value]) => !value)
    .map(([key]) => key);
  if (missing.length > 0) throw new FirebaseConfigMissing(missing);
  // Next's fast refresh re-runs modules; initializeApp twice throws.
  return getApps()[0] ?? initializeApp(config as Required<typeof config>);
}

async function getAuthInstance(): Promise<Auth> {
  const auth = getAuth(getFirebaseApp());
  // Must precede sign-in: this is what stops the ID token reaching IndexedDB.
  await setPersistence(auth, inMemoryPersistence);
  return auth;
}

/**
 * Run the Google popup and return a fresh ID token.
 *
 * The caller is responsible for exchanging it and then calling `endFirebaseSession()`.
 * Nothing here persists any credential.
 */
export async function signInWithGoogle(): Promise<string> {
  const auth = await getAuthInstance();
  const provider = new GoogleAuthProvider();
  // Always show the chooser: without it, a browser with one Google session silently
  // reuses it, which is confusing on a shared machine and makes sign-out feel broken.
  provider.setCustomParameters({ prompt: "select_account" });
  const credential = await signInWithPopup(auth, provider);
  return credential.user.getIdToken();
}

/** Drop the Firebase session once its token has been exchanged for an Aegis cookie. */
export async function endFirebaseSession(): Promise<void> {
  try {
    await signOut(await getAuthInstance());
  } catch {
    // In-memory persistence means there is nothing durable to clean up, so a failure here
    // cannot leave a credential behind. Never let it mask the outcome of the exchange.
  }
}

/** Human-readable reason for a failed popup, for the login screen. */
export function describeSignInError(error: unknown): string {
  const code = (error as { code?: string })?.code ?? "";
  switch (code) {
    case "auth/popup-closed-by-user":
    case "auth/cancelled-popup-request":
      return "Sign-in was cancelled.";
    case "auth/popup-blocked":
      return "Your browser blocked the sign-in popup. Allow popups for this site and retry.";
    case "auth/network-request-failed":
      return "Could not reach Google. Check your connection and retry.";
    case "auth/unauthorized-domain":
      return "This domain is not authorised in the Firebase console (Authentication → Settings → Authorized domains).";
    case "auth/operation-not-allowed":
      return "Google sign-in is not enabled for this Firebase project (Authentication → Sign-in method).";
    default:
      if (error instanceof FirebaseConfigMissing) return error.message;
      return error instanceof Error ? error.message : "Sign-in failed.";
  }
}
