package server

// Worker knowledge base file inspection
// (GET /api/v1/workers/{name}/workspace-files/...).
//
// Each worker's qwenpaw app (QwenPaw >= 2.1) exposes read-only workspace
// file endpoints on :8088 (0.0.0.0 listen; no auth in worker context
// because no console user is registered): /workspace/tree (paginated
// directory listing), /workspace/file-metadata and /workspace/file-content
// (bounded UTF-8 chunk reads). The Controller proxies those three
// read-only subpaths so L2 humans and the workbench plugin can inspect a
// worker's knowledge base (MEMORY.md, memory/**, digest/**) without
// reaching into the docker network directly.
//
// Embedded mode only: the worker app is reachable by container name inside
// the shared docker network. The effective container name prefix comes from
// configuration (AGENTTEAMS_PROXY_CONTAINER_PREFIX, or derived from
// AGENTTEAMS_RESOURCE_PREFIX when auto-prefixing is enabled; empty when
// auto-prefixing is disabled), and the port is the effective console port
// resolved through the same system-wins env chain used at container
// creation (service.EffectiveWorkerConsolePort — a conflicting spec.env
// value is discarded, so the container always listens on 8088). In kube
// mode there is no stable in-cluster DNS name for the worker pod, so the
// endpoints return 503.
//
// Two independent path boundaries:
//
//   - The upstream app hardens path resolution (no absolute paths, no
//     ".." segments, no NUL bytes, no symlink escape outside the workspace
//     root) and hides dot entries in directory listings — but a directly
//     requested path still resolves, so dot directories (.copaw/agent.json
//     carries the worker's Matrix token and MinIO credentials) remain
//     reachable upstream.
//
//   - This handler therefore enforces its own allowlist on top: only
//     MEMORY.md, memory/** and digest/** are addressable. The allowlist is
//     a prefix allowlist on exact root names (memory, digest) plus the
//     single top-level file MEMORY.md — never a denylist — so any other
//     workspace content (SOUL.md, PROFILE.md, TODO.md, .copaw/, .qwenpaw/,
//     checkpoints/, skills/, ...) is rejected before the request reaches
//     the worker.
//
// The upstream root=workspace parameter (the agent's own storage root, as
// opposed to root=project, the primary bound project directory) is pinned
// server-side and is never part of the client-facing query surface.
//
// Fixed-path forwarding only (tree / file-metadata / file-content, plus
// their whitelisted queries) — never a generic reverse proxy, and never a
// write endpoint, so the attack surface is limited to three read-only
// QwenPaw endpoints.

import (
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	v1beta1 "github.com/agentscope-ai/AgentTeams/agentteams-controller/api/v1beta1"
	authpkg "github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/auth"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/httputil"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/service"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	// workspaceFilesProxyTimeout bounds each upstream call.
	workspaceFilesProxyTimeout = 5 * time.Second

	// kbPathMaxSegments bounds the depth of addressable knowledge paths
	// (memory/2026-08-31/topic.md is three segments; four leaves margin
	// for one more nesting level without opening the full workspace tree).
	kbPathMaxSegments = 4

	// kbSegmentMaxBytes matches the upstream per-segment limit
	// (QwenPaw workspace_files._validate_segment).
	kbSegmentMaxBytes = 255

	// kbMaxPageLimit mirrors the upstream tree pagination cap (QwenPaw
	// workspace_files.MAX_PAGE_SIZE).
	kbMaxPageLimit = 500

	// kbMaxFileLimit mirrors the upstream file-content chunk cap (QwenPaw
	// workspace_files.MAX_CHUNK_SIZE).
	kbMaxFileLimit = 1024 * 1024
)

// workspaceFileSubpaths is the fixed whitelist of forwardable QwenPaw
// endpoints. Write endpoints (file-content PUT, file-upload) and binary
// streaming (file-download) are deliberately absent.
var workspaceFileSubpaths = map[string]bool{
	"tree":          true,
	"file-metadata": true,
	"file-content":  true,
}

// kbFileRoots are the top-level single files addressable by the
// file-metadata / file-content subpaths.
var kbFileRoots = []string{"MEMORY.md"}

// kbDirRoots are the knowledge base directories addressable by all three
// subpaths (tree on the directory or any of its subpaths; the file
// subpaths on files below it). Matching is on the exact first segment, so
// memories/ and memoryX/ are not prefixes of memory/.
var kbDirRoots = []string{"memory", "digest"}

// validateKbPath enforces the knowledge base allowlist (see the package
// comment). forFile selects the file subpaths (file-metadata /
// file-content); the tree subpath takes directory paths.
func validateKbPath(path string, forFile bool) error {
	if path == "" {
		return errors.New("path is required (memory/ or digest/)")
	}
	if strings.ContainsRune(path, '\\') || strings.ContainsRune(path, 0) || strings.HasPrefix(path, "/") {
		return errors.New("path must be a relative POSIX path")
	}
	segments := strings.Split(path, "/")
	if len(segments) > kbPathMaxSegments {
		return errors.New("path is too deep")
	}
	for _, seg := range segments {
		if seg == "" || seg == "." || seg == ".." {
			return errors.New("path contains an invalid segment")
		}
		if strings.HasPrefix(seg, ".") {
			return errors.New("hidden paths are not accessible")
		}
		if len(seg) > kbSegmentMaxBytes {
			return errors.New("path segment is too long")
		}
	}
	first := segments[0]
	if forFile {
		for _, root := range kbFileRoots {
			if first == root {
				return nil
			}
		}
	}
	for _, root := range kbDirRoots {
		if first == root {
			return nil
		}
	}
	return errors.New("path is not in the knowledge base allowlist")
}

// validateWorkspaceFilesQuery enforces the strict per-subpath query
// whitelist and returns the upstream query string with root=workspace
// pinned. Unknown or duplicate parameters are rejected rather than
// silently dropped so client mistakes surface immediately (the same
// semantics the checkpoint proxy enforces).
func validateWorkspaceFilesQuery(sub string, q url.Values) (string, error) {
	var allowed map[string]bool
	switch sub {
	case "tree":
		allowed = map[string]bool{"path": true, "cursor": true, "limit": true}
	case "file-metadata":
		allowed = map[string]bool{"path": true}
	case "file-content":
		allowed = map[string]bool{"path": true, "offset": true, "limit": true}
	}
	for key, vals := range q {
		if !allowed[key] {
			return "", fmt.Errorf("unsupported query parameter: %s", key)
		}
		if len(vals) > 1 {
			return "", fmt.Errorf("duplicate query parameter: %s", key)
		}
	}
	if err := validateKbPath(q.Get("path"), sub != "tree"); err != nil {
		return "", err
	}
	if sub == "tree" || sub == "file-content" {
		if raw := q.Get("limit"); raw != "" {
			maxLimit := kbMaxFileLimit
			if sub == "tree" {
				maxLimit = kbMaxPageLimit
			}
			limit, err := strconv.Atoi(raw)
			if err != nil || limit < 1 || limit > maxLimit {
				return "", fmt.Errorf("limit must be an integer between 1 and %d", maxLimit)
			}
		}
	}
	if sub == "file-content" {
		if raw := q.Get("offset"); raw != "" {
			offset, err := strconv.Atoi(raw)
			if err != nil || offset < 0 {
				return "", errors.New("offset must be a non-negative integer")
			}
		}
	}
	// Rebuild the upstream query from the validated values only, in a
	// fixed order, and pin the root. The client's raw query string is
	// never forwarded verbatim.
	up := url.Values{}
	up.Set("path", q.Get("path"))
	if raw := q.Get("cursor"); raw != "" {
		up.Set("cursor", raw)
	}
	if raw := q.Get("limit"); raw != "" {
		up.Set("limit", raw)
	}
	if raw := q.Get("offset"); raw != "" {
		up.Set("offset", raw)
	}
	up.Set("root", "workspace")
	return up.Encode(), nil
}

// WorkspaceFilesHandler proxies worker knowledge base read endpoints.
type WorkspaceFilesHandler struct {
	client    client.Client
	namespace string
	kubeMode  string
	http      *http.Client
	// containerPrefix is the effective worker container name prefix — the
	// same value the docker backend uses for container naming (derived from
	// AGENTTEAMS_PROXY_CONTAINER_PREFIX / AGENTTEAMS_RESOURCE_PREFIX /
	// auto-prefix; empty when auto-prefixing is disabled).
	containerPrefix string
	// workerBaseURL resolves a worker name to its qwenpaw app base URL from
	// the effective prefix and the worker's env. Injectable for tests.
	workerBaseURL func(name string, env map[string]string) string
}

// NewWorkspaceFilesHandler creates the handler with the default
// embedded-mode worker address resolution. containerPrefix must be the
// effective prefix from controller configuration (see
// config.ContainerPrefix).
func NewWorkspaceFilesHandler(c client.Client, namespace, kubeMode, containerPrefix string) *WorkspaceFilesHandler {
	h := &WorkspaceFilesHandler{
		client:          c,
		namespace:       namespace,
		kubeMode:        kubeMode,
		http:            &http.Client{Timeout: workspaceFilesProxyTimeout},
		containerPrefix: containerPrefix,
	}
	h.workerBaseURL = h.defaultWorkerBaseURL
	return h
}

// defaultWorkerBaseURL resolves a worker's qwenpaw app base URL from the
// effective container prefix and the effective console port. The port goes
// through service.EffectiveWorkerConsolePort — the same system-wins env
// chain used at container creation — so the proxy can never target a port
// the container does not listen on (a conflicting spec.env value is
// discarded before the container is created, so the raw spec.env must not
// be read here).
func (h *WorkspaceFilesHandler) defaultWorkerBaseURL(name string, env map[string]string) string {
	port := service.EffectiveWorkerConsolePort(env)
	return fmt.Sprintf("http://%s%s:%s", h.containerPrefix, name, port)
}

// proxyWorkspaceFiles handles GET /api/v1/workers/{name}/workspace-files/{sub}.
// Scoped callers (team leaders / L2 humans) may only inspect workers in the
// teams they control — mirrors GET /api/v1/workers/{name} and the
// checkpoint proxy (W8: 404, not 403, so worker existence cannot be
// probed).
func (h *WorkspaceFilesHandler) proxyWorkspaceFiles(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	sub := r.PathValue("sub")
	if name == "" || !workerNamePattern.MatchString(name) {
		httputil.WriteError(w, http.StatusBadRequest, "worker name is required and must be a valid DNS label")
		return
	}
	if !workspaceFileSubpaths[sub] {
		httputil.WriteError(w, http.StatusBadRequest, "unsupported workspace file subpath")
		return
	}
	// Kube-mode check runs before any worker lookup: the endpoints are
	// entirely unavailable in kube mode, and a uniform 503 (rather than a
	// per-worker 404 vs 503 split) avoids leaking worker existence.
	if h.kubeMode != "embedded" {
		httputil.WriteError(w, http.StatusServiceUnavailable, "worker workspace file inspection requires embedded mode")
		return
	}

	var worker v1beta1.Worker
	if err := h.client.Get(r.Context(), client.ObjectKey{Name: name, Namespace: h.namespace}, &worker); err != nil {
		if apierrors.IsNotFound(err) {
			httputil.WriteError(w, http.StatusNotFound, "worker not found")
			return
		}
		writeK8sError(w, "get worker workspace files", err)
		return
	}
	// Resolve the owning team for the scoped-caller check (same chain as
	// ResourceHandler.GetWorker and the checkpoint proxy: standalone
	// workers hide as 404 for scoped callers).
	teamObj, _, _, err := findTeamMember(r.Context(), h.client, h.namespace, name)
	if err != nil {
		writeK8sError(w, "get worker workspace files", err)
		return
	}
	// Note: findTeamMember's second return value is the member (worker)
	// name, not the team name — the scoped check must compare against the
	// Team CR name (see ResourceHandler.GetWorker).
	teamName := ""
	if teamObj != nil {
		teamName = teamObj.Name
	}
	if caller := authpkg.CallerFromContext(r.Context()); caller != nil &&
		(caller.Role == authpkg.RoleTeamLeader || caller.Role == authpkg.RoleHuman) &&
		!caller.TeamMatches(teamName) {
		httputil.WriteError(w, http.StatusNotFound, "worker not found")
		return
	}

	query, err := validateWorkspaceFilesQuery(sub, r.URL.Query())
	if err != nil {
		httputil.WriteError(w, http.StatusBadRequest, err.Error())
		return
	}

	target := h.workerBaseURL(name, worker.Spec.Env) + "/workspace/" + sub
	if query != "" {
		target += "?" + query
	}

	req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, target, nil)
	if err != nil {
		httputil.WriteError(w, http.StatusInternalServerError, "build workspace files request: "+err.Error())
		return
	}
	resp, err := h.http.Do(req)
	if err != nil {
		// Connection refused (worker stopped), DNS failure, timeout.
		httputil.WriteError(w, http.StatusBadGateway, "worker workspace API unreachable")
		return
	}
	defer resp.Body.Close()

	switch resp.StatusCode {
	case http.StatusOK:
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = io.Copy(w, resp.Body)
	case http.StatusBadRequest, http.StatusNotFound, http.StatusConflict, http.StatusRequestedRangeNotSatisfiable:
		// Pass through verbatim: invalid cursor/offset (400), file not
		// found — which is also the pre-2.1 router-missing signal, see the
		// documentation's MEMORY.md probe heuristic (404), file changed
		// while being read (409), or offset beyond end of file (416).
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(resp.StatusCode)
		_, _ = w.Write(body)
	default:
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		httputil.WriteError(w, http.StatusBadGateway, fmt.Sprintf("workspace files API error (status %d): %s", resp.StatusCode, string(body)))
	}
}
