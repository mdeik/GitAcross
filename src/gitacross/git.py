import io
import logging
import os
import re
import subprocess
import tarfile
from pathlib import Path

logger = logging.getLogger(__name__)

# scheme://user:token@host — mask the token when logging URLs
_TOKEN_RE = re.compile(r"(://[^/@\s:]+):[^/@\s]+@")


def _redact(value):
    """Mask credentials embedded in URLs (scheme://user:token@ → scheme://user:***@)."""
    return _TOKEN_RE.sub(r"\1:***@", value)


def _log_git_stderr(command, returncode, stderr, level):
    """Log a failed git command's stderr (decoded, redacted)."""
    if not stderr:
        return
    msg = stderr.strip()
    if not isinstance(msg, str):
        msg = msg.decode(errors="replace")
    if msg:
        logger.log(
            level,
            "git %s failed (exit %d): %s",
            command,
            returncode,
            _redact(msg),
        )


def _git(*args, check=True, input_data=None, text=True, env=None):
    cmd = ["git"] + [str(a) for a in args]
    logger.debug("> git %s", _redact(" ".join(str(a) for a in args)))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=text,
            check=check,
            input=input_data,
            env=env,
        )
    except subprocess.CalledProcessError as e:
        _log_git_stderr(args[0], e.returncode, e.stderr, level=logging.ERROR)
        # Sanitize the command shown in the traceback (it may contain a token URL)
        for i, part in enumerate(e.cmd):
            if isinstance(part, str):
                e.cmd[i] = _redact(part)
        raise
    if result.returncode != 0:
        # check=False path — callers inspect the return code themselves
        _log_git_stderr(args[0], result.returncode, result.stderr, level=logging.DEBUG)
    return result


class GitRepo:
    """A git repository for release operations.

    Works with both bare mirrors (cloned from remote) and local repos.
    Uses `--git-dir` + `--work-tree` for committing from a temp directory,
    avoiding any git worktree management.
    """

    def __init__(self, git_dir, is_bare=False):
        self.git_dir = Path(git_dir)
        self.is_bare = is_bare

    @classmethod
    def ensure_mirror(cls, url, dest):
        """Clone a bare mirror or update an existing one.

        Fetches with --prune: a failed push leaves locally created refs that
        the remote never accepted; pruning resets the mirror to the remote's
        actual state so the next run re-syncs instead of treating those
        stale refs as already-pushed.
        """
        path = Path(dest)
        if path.exists():
            # Update remote URL *before* fetching so token rotation takes effect
            current = _git("-C", str(path), "remote", "get-url", "origin", check=False)
            if current.returncode == 0 and current.stdout.strip() != url:
                _git("-C", str(path), "remote", "set-url", "origin", url)
                logger.info("Updated remote URL for mirror at %s", dest)
            _git("-C", str(path), "fetch", "--tags", "--prune", "origin")
            logger.info("Updated mirror at %s", dest)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            _git("clone", "--mirror", url, str(path))
            logger.info("Cloned mirror from %s", _redact(url))
        # Ensure author identity for automated commits
        _git("-C", str(path), "config", "user.name", "GitAcross")
        _git("-C", str(path), "config", "user.email", "sync@gitacross")
        return cls(path, is_bare=True)

    @classmethod
    def local(cls, path):
        """Open an existing local git repository."""
        p = Path(path)
        result = _git("-C", str(p), "rev-parse", "--git-dir")
        git_dir = p / result.stdout.strip()
        if not git_dir.exists():
            raise ValueError(f"Not a git repository: {path}")
        return cls(git_dir, is_bare=False)

    def _g(self, *args, text=True, env=None, **kwargs):
        """Run git command with --git-dir set."""
        return _git("--git-dir", str(self.git_dir), *args, text=text, env=env, **kwargs)

    def _gw(self, work_dir, *args, env=None, **kwargs):
        """Run git command with --git-dir and --work-tree set."""
        return _git(
            "--git-dir",
            str(self.git_dir),
            "--work-tree",
            str(work_dir),
            *args,
            env=env,
            **kwargs,
        )

    def list_tags(self):
        """Return all tag names in the repo."""
        result = self._g("tag", "-l")
        raw = result.stdout.strip()
        return raw.split("\n") if raw else []

    def list_tags_sorted_by_date(self):
        """Return tag names sorted by their commit date (oldest first).

        Uses creatordate which works for both annotated (taggerdate)
        and lightweight (committerdate) tags.
        """
        result = self._g(
            "for-each-ref",
            "--sort=creatordate",
            "--format=%(refname:short)",
            "refs/tags/",
        )
        raw = result.stdout.strip()
        return raw.split("\n") if raw else []

    def tag_commit_date(self, tag):
        """Return the ISO-8601 commit date for a tag."""
        result = self._g("log", "-1", "--format=%cI", tag, check=False)
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def resolve_commit(self, ref):
        """Return the 40-char commit SHA for a ref, tag, or commitish."""
        if not ref:
            return ""
        result = self._g("rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
        if result.returncode == 0:
            return result.stdout.strip()
        return ""

    def export_commit(self, commit_sha, dest):
        """Export a commit's file tree into *dest* (empty dir recommended)."""
        result = self._g("archive", commit_sha, "--format=tar", text=False)
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r|") as tar:
            tar.extractall(path=str(dest))

    def export_tag(self, tag, dest):
        """Export a tag's file tree into *dest* (empty dir recommended)."""
        commit_sha = self.resolve_commit(tag) or tag
        self.export_commit(commit_sha, dest)

    def ensure_branch(self, branch):
        """Ensure HEAD points to *branch*, creating it if needed.

        Priority: local branch → remote tracking → new branch.
        """
        # Already exists locally
        local = self._g("show-ref", "--verify", f"refs/heads/{branch}", check=False)
        if local.returncode == 0:
            self._g("symbolic-ref", "HEAD", f"refs/heads/{branch}")
            return

        # Exists on remote
        remote = self._g(
            "show-ref", "--verify", f"refs/remotes/origin/{branch}", check=False
        )
        if remote.returncode == 0:
            self._g("branch", "--force", branch, f"origin/{branch}")
            self._g("symbolic-ref", "HEAD", f"refs/heads/{branch}")
            return

        # Brand new branch — just set HEAD, the ref is created on first commit
        self._g("symbolic-ref", "HEAD", f"refs/heads/{branch}")

    def commit(self, work_dir, message, date=None, author_name=None, author_email=None):
        """Stage all files in *work_dir* and commit on current branch.

        If *date* is provided (ISO-8601 string), sets both author and committer
        dates so the commit appears at the correct chronological position.

        If *author_name* and/or *author_email* are provided, they override the
        git identity for this commit (both author and committer).
        """
        self._gw(work_dir, "add", "-A")
        env = dict(os.environ)
        if date:
            env.update({
                "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_DATE": date,
            })
        if author_name:
            env.update({
                "GIT_AUTHOR_NAME": author_name,
                "GIT_COMMITTER_NAME": author_name,
            })
        if author_email:
            env.update({
                "GIT_AUTHOR_EMAIL": author_email,
                "GIT_COMMITTER_EMAIL": author_email,
            })
        if not date and not author_name and not author_email:
            env = None
        result = self._gw(
            work_dir,
            "commit",
            "--no-verify",
            "-m",
            message,
            check=False,
            env=env,
        )
        # Exit code 1 from `git commit` means "nothing to commit" (defined contract)
        if result.returncode not in (0, 1):
            result.check_returncode()
        return result

    def tag(self, name, message):
        """Create (or force-update) an annotated tag."""
        self._g("tag", "-f", name, "-m", message)

    def reset_worktree(self):
        """Populate the working tree to match HEAD. No-op for bare repos."""
        if not self.is_bare:
            _git("-C", str(self.git_dir.parent), "reset", "--hard", "HEAD")

    def push(self, remote, *refs):
        """Push specific refs to remote."""
        # Mirrors (cloned with --mirror) reject explicit refspecs by default.
        # Temporarily disable mirror mode so we can push only the refs we need.
        if self.is_bare:
            self._g("config", "--local", "remote.origin.mirror", "false")
            try:
                self._g("push", remote, *refs)
            finally:
                self._g("config", "--local", "remote.origin.mirror", "true")
        else:
            self._g("push", remote, *refs)

    def tag_exists(self, tag):
        """Check if a tag exists locally."""
        result = self._g("show-ref", "--verify", f"refs/tags/{tag}", check=False)
        return result.returncode == 0

    def head_sha(self):
        """Return the SHA of HEAD."""
        result = self._g("rev-parse", "HEAD")
        return result.stdout.strip()

    def is_ancestor(self, older_sha, newer_sha):
        """Return True if *older_sha* is a (strict) ancestor of *newer_sha*.

        Uses ``git merge-base --is-ancestor`` which exits 0 when true and 1
        when false. Returns False for any git error (e.g. unknown ref).
        """
        result = self._g(
            "merge-base", "--is-ancestor", older_sha, newer_sha, check=False
        )
        return result.returncode == 0

    def resolve_default_branch_head(self, branch=None):
        """Return the commit SHA at the tip of the default (or given) branch.

        Supports both bare mirrors (where refs usually live under ``refs/heads/``
        and ``HEAD`` points to the default branch) and remote-tracking setups
        (``refs/remotes/origin/``).
        """
        if branch:
            candidates = [
                f"refs/heads/{branch}",
                f"refs/remotes/origin/{branch}",
                f"refs/remotes/{branch}",
                branch,
            ]
            for ref in candidates:
                result = self._g("rev-parse", "--verify", ref, check=False)
                if result.returncode == 0:
                    return result.stdout.strip()
            logger.warning("Could not resolve branch '%s' in %s", branch, self.git_dir)
            return ""

        # Auto-detect default branch:
        # 1. Try symbolic-ref HEAD (works on bare mirrors and non-bare working copies)
        result = self._g("symbolic-ref", "--short", "HEAD", check=False)
        if result.returncode == 0:
            branch_name = result.stdout.strip()
            for ref in (f"refs/heads/{branch_name}", branch_name, "HEAD"):
                r = self._g("rev-parse", "--verify", ref, check=False)
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout.strip()

        # 2. Try refs/remotes/origin/HEAD
        result = self._g(
            "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False
        )
        if result.returncode == 0:
            remote_branch = result.stdout.strip()  # e.g. "origin/main"
            ref = f"refs/remotes/{remote_branch}"
            r = self._g("rev-parse", "--verify", ref, check=False)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()

        # 3. Fall back to common branch names in refs/heads/ or refs/remotes/origin/
        for candidate in ("main", "master", "trunk", "development", "dev", "default"):
            for ref in (f"refs/heads/{candidate}", f"refs/remotes/origin/{candidate}"):
                r = self._g("rev-parse", "--verify", ref, check=False)
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout.strip()

        # 4. Fall back to direct HEAD rev-parse
        r = self._g("rev-parse", "--verify", "HEAD", check=False)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()

        logger.warning(
            "Could not determine default branch for repository at %s",
            self.git_dir,
        )
        return ""

