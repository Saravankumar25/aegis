'use client';

import { useEffect, useRef, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { AlertTriangle } from 'lucide-react';
import { useAuth } from '@/components/auth/AuthProvider';
import {
  sendPasswordReset,
  signInWithEmail,
  signInWithGoogle,
  signUpWithEmail,
} from '@/lib/auth';
import { SignInForms } from './SignInForms';

/**
 * The sign-in panel.
 *
 * Presented as an operational entry point rather than a marketing call to
 * action: this is the door to a system that can change production, so it states
 * plainly which authentication methods this deployment actually supports rather
 * than offering buttons that cannot work.
 *
 * Failure messages say what an operator can do about it. "Invalid credentials"
 * is useless at 3am; "Firebase is not configured for this deployment" tells
 * them where to look.
 */
export function SignInPanel() {
  const router = useRouter();
  const params = useSearchParams();
  const { status, modes, refresh } = useAuth();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [mode, setMode] = useState<'sign-in' | 'sign-up' | 'reset'>('sign-in');
  const [busy, setBusy] = useState<null | 'google' | 'email' | 'reset'>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const headingRef = useRef<HTMLHeadingElement>(null);

  const hasFirebase = modes.includes('firebase');
  const expired = params.get('expired') === '1';
  const heading = mode === 'sign-up'
    ? 'Create your Aegis account'
    : mode === 'reset'
      ? 'Reset your password'
      : 'Sign in to Aegis';

  // An operator who is already signed in should never be asked again.
  useEffect(() => {
    if (status === 'authenticated') router.replace('/overview');
  }, [status, router]);

  async function run(kind: 'google' | 'email', fn: () => Promise<unknown>) {
    setBusy(kind);
    setError('');
    try {
      await fn();
      refresh();
      router.replace('/overview');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign-in failed.');
    } finally {
      setBusy(null);
    }
  }

  async function submitEmail() {
    if (mode === 'reset') {
      setBusy('reset');
      setError('');
      setNotice('');
      try {
        await sendPasswordReset(email);
        setNotice('Password reset email sent. Check your inbox, then return to sign in.');
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not send the reset email.');
      } finally {
        setBusy(null);
      }
      return;
    }
    await run('email', () => mode === 'sign-up'
      ? signUpWithEmail(email, password)
      : signInWithEmail(email, password));
  }

  return (
    <div className="flex h-full flex-col justify-center gap-6 px-8 py-12 xl:px-12">
      <header className="space-y-2">
        <h2
          ref={headingRef}
          tabIndex={-1}
          className="text-h2 font-bold tracking-tight text-ink-primary focus:outline-none"
        >
          {heading}
        </h2>
        <p className="text-body font-medium leading-relaxed text-ink-secondary">
          Access the control plane. Every action you take is attributed to you
          and recorded in the audit trail.
        </p>
      </header>

      {expired && (
        <div
          role="status"
          className="flex items-start gap-2.5 rounded-card border border-status-warning/30
                     bg-status-warning/[0.07] px-3.5 py-3"
        >
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-status-warning" aria-hidden />
          <p className="text-meta font-medium leading-relaxed text-ink-secondary">
            Your session expired and you were signed out. Nothing was lost.
          </p>
        </div>
      )}

      {error && (
        <div
          role="alert"
          className="flex items-start gap-2.5 rounded-card border border-status-critical/30
                     bg-status-critical/[0.07] px-3.5 py-3"
        >
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-status-critical" aria-hidden />
          <p className="text-meta font-medium leading-relaxed text-ink-primary">{error}</p>
        </div>
      )}

      {notice && (
        <div role="status" className="rounded-card border border-status-ok/30 bg-status-ok/[0.07] px-3.5 py-3">
          <p className="text-meta font-medium leading-relaxed text-ink-primary">{notice}</p>
        </div>
      )}

      {!hasFirebase && (
        <div role="alert" className="rounded-card border border-line bg-surface-2 px-3.5 py-3">
          <p className="text-meta font-semibold text-ink-primary">
            No sign-in method is configured
          </p>
          <p className="mt-1.5 text-meta font-medium leading-relaxed text-ink-tertiary">
            Set the <code className="font-mono text-ink-secondary">NEXT_PUBLIC_FIREBASE_*</code>{' '}
            variables and rebuild the web image. Local development uses the
            Firebase Auth emulator, never a token pasted into this public page.
          </p>
        </div>
      )}

      <SignInForms
        hasFirebase={hasFirebase}
        mode={mode}
        busy={busy}
        email={email}
        password={password}
        onEmailChange={setEmail}
        onPasswordChange={setPassword}
        onGoogle={() => void run('google', signInWithGoogle)}
        onEmail={() => void submitEmail()}
        onModeChange={(next) => {
          setMode(next);
          setError('');
          setNotice('');
          window.requestAnimationFrame(() => headingRef.current?.focus());
        }}
      />

      <p className="text-meta font-medium leading-relaxed text-ink-tertiary">
        Aegis never takes a production action without either a deterministic
        policy allowing it or a human approving it. Your identity is what makes
        that record meaningful.
      </p>
    </div>
  );
}
