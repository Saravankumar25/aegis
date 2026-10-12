'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  type Identity,
  availableModes,
  clearSession,
  observeFirebaseSession,
  readSession,
  signOut as doSignOut,
} from '@/lib/auth';
import { onUnauthorized } from '@/lib/api';

/**
 * Session state for the whole console.
 *
 * `status` has three values rather than a boolean because the difference
 * matters visually: while we are still reading storage we must render neither
 * the landing page nor the console, or every reload flashes a sign-in screen at
 * an operator who is already authenticated.
 */
type Status = 'loading' | 'authenticated' | 'anonymous';

interface AuthState {
  status: Status;
  identity: Identity | null;
  modes: ReturnType<typeof availableModes>;
  refresh: () => void;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [status, setStatus] = useState<Status>('loading');
  const [identity, setIdentity] = useState<Identity | null>(null);

  const refresh = useCallback(() => {
    const { token, identity: stored } = readSession();
    setIdentity(stored);
    setStatus(token ? 'authenticated' : 'anonymous');
  }, []);

  // Firebase owns persistence. Its observer refreshes the ID token on reload
  // and whenever the SDK rotates it; localStorage is only the API client's
  // immediate cache, never the authority for whether a session is still live.
  useEffect(() => {
    let active = true;
    let unsubscribe = () => {};
    void observeFirebaseSession((session) => {
      if (!active) return;
      setIdentity(session?.identity ?? null);
      setStatus(session ? 'authenticated' : 'anonymous');
    }).then((stop) => {
      if (active) unsubscribe = stop;
      else stop();
    }).catch(() => {
      if (!active) return;
      clearSession();
      setIdentity(null);
      setStatus('anonymous');
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  /**
   * A 401 from any request ends the session immediately.
   *
   * An expired Firebase token otherwise leaves the console showing stale data
   * behind a series of silent failures, which reads as "Aegis is broken" rather
   * than "you need to sign in again".
   */
  useEffect(() => {
    return onUnauthorized(() => {
      void doSignOut().finally(() => {
        setIdentity(null);
        setStatus('anonymous');
        router.replace('/?expired=1');
      });
    });
  }, [router]);

  const signOut = useCallback(async () => {
    await doSignOut();
    setIdentity(null);
    setStatus('anonymous');
    router.replace('/');
  }, [router]);

  const value = useMemo<AuthState>(
    () => ({ status, identity, modes: availableModes(), refresh, signOut }),
    [status, identity, refresh, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>');
  return ctx;
}
