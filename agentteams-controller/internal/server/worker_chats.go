// The worker chats proxy exposes a read-only view of a worker's conversation
// history (its qwenpaw chats) for the dashboard and other API clients. It is
// the same thin, byte-transparent pattern as the worker checkpoint proxy: the
// controller resolves the worker, enforces the worker-scoped read boundary
// (W8: 404, not 403, so worker existence cannot be probed), and forwards to
// the worker's own qwenpaw app over loopback.
//
// Embedded mode only: the proxy targets the per-worker console app, which
// the Kubernetes deployment model does not expose per worker, so the route
// 503s uniformly there (same rationale as the checkpoint proxy).
package server

import (
	"fmt"
	"io"
	"net/http"
	"regexp"
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
	// chatProxyTimeout bounds each upstream call to the worker's qwenpaw
	// app (same bound as the checkpoint proxy).
	chatProxyTimeout = 5 * time.Second

	// chatErrorBodyCap limits how much of an upstream 5xx body is surfaced
	// in the wrapped 502 response.
	chatErrorBodyCap = 128
)

// chatIDPattern matches qwenpaw chat ids — every chat is created with
// str(uuid4()) (lowercase hex and hyphens), the same family as
// workerNamePattern. It also blocks path separators, query characters, and
// other input that could be injected into the upstream URL.
var chatIDPattern = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`)

// chatListQueryWhitelist is the documented read-only filter set of the
// upstream list endpoint. Unknown parameters are rejected with 400 rather
// than forwarded (the checkpoint proxy's query discipline: never 422).
// include_app_owned is a 2.2.1+ filter; older runtimes silently ignore the
// unknown parameter, so forwarding it is harmless across versions.
var chatListQueryWhitelist = map[string]bool{
	"user_id":           true,
	"channel":           true,
	"archived":          true,
	"include_app_owned": true,
}

// ChatsHandler proxies the worker's read-only chat (session) endpoints.
type ChatsHandler struct {
	client          client.Client
	namespace       string
	kubeMode        string
	http            *http.Client
	containerPrefix string
	workerBaseURL   func(name string, env map[string]string) string
}

// NewChatsHandler builds a chats proxy over the given K8s client.
// containerPrefix must be the effective prefix from controller
// configuration (see config.ContainerPrefix).
func NewChatsHandler(c client.Client, namespace, kubeMode, containerPrefix string) *ChatsHandler {
	h := &ChatsHandler{
		client:          c,
		namespace:       namespace,
		kubeMode:        kubeMode,
		http:            &http.Client{Timeout: chatProxyTimeout},
		containerPrefix: containerPrefix,
	}
	h.workerBaseURL = h.defaultWorkerBaseURL
	return h
}

// defaultWorkerBaseURL resolves a worker's qwenpaw app base URL from the
// effective container prefix and the effective console port. The port goes
// through service.EffectiveWorkerConsolePort — the same system-wins env
// chain used at container creation — so the proxy can never target a port
// the container does not listen on.
func (h *ChatsHandler) defaultWorkerBaseURL(name string, env map[string]string) string {
	port := service.EffectiveWorkerConsolePort(env)
	return fmt.Sprintf("http://%s%s:%s", h.containerPrefix, name, port)
}

// proxy performs the shared request pipeline for all three routes:
// worker-name validation, the kube-mode gate, worker resolution, the
// worker-scoped read boundary (W8), the upstream dial, and the status
// mapping. upstreamPath is the qwenpaw path to forward (already validated
// by the caller — worker name and chat id are pattern-checked before the
// dial, so neither can inject path segments).
func (h *ChatsHandler) proxy(w http.ResponseWriter, r *http.Request, name, upstreamPath string) {
	if name == "" || !workerNamePattern.MatchString(name) {
		httputil.WriteError(w, http.StatusBadRequest, "worker name is required and must be a valid DNS label")
		return
	}
	// Kube-mode check runs before any worker lookup: the endpoints are
	// entirely unavailable in kube mode, and a uniform 503 (rather than a
	// per-worker 404 vs 503 split) avoids leaking worker existence.
	if h.kubeMode != "embedded" {
		httputil.WriteError(w, http.StatusServiceUnavailable, "worker session inspection requires embedded mode")
		return
	}

	var worker v1beta1.Worker
	if err := h.client.Get(r.Context(), client.ObjectKey{Name: name, Namespace: h.namespace}, &worker); err != nil {
		if apierrors.IsNotFound(err) {
			httputil.WriteError(w, http.StatusNotFound, "worker not found")
			return
		}
		writeK8sError(w, "get worker chats", err)
		return
	}
	// Resolve the owning team for the scoped-caller check (same chain as
	// ResourceHandler.GetWorker: standalone workers hide as 404). Note:
	// findTeamMember's second return value is the member (worker) name,
	// not the team name — the check must compare against the Team CR name.
	teamObj, _, _, err := findTeamMember(r.Context(), h.client, h.namespace, name)
	if err != nil {
		writeK8sError(w, "get worker chats", err)
		return
	}
	teamName := ""
	if teamObj != nil {
		teamName = teamObj.Name
	}
	// Scoped callers (team leaders / L2 humans) may only inspect workers in
	// the teams they control — mirrors GET /api/v1/workers/{name} (W8: 404,
	// not 403, so worker existence cannot be probed).
	if caller := authpkg.CallerFromContext(r.Context()); caller != nil &&
		(caller.Role == authpkg.RoleTeamLeader || caller.Role == authpkg.RoleHuman) &&
		!caller.TeamMatches(teamName) {
		httputil.WriteError(w, http.StatusNotFound, "worker not found")
		return
	}

	base := h.workerBaseURL(worker.Name, worker.Spec.Env)
	req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, base+upstreamPath, nil)
	if err != nil {
		httputil.WriteError(w, http.StatusInternalServerError, "failed to build upstream request")
		return
	}

	resp, err := h.http.Do(req)
	if err != nil {
		httputil.WriteError(w, http.StatusBadGateway, "worker unreachable")
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		// Chat histories can be large (full message transcripts); stream
		// verbatim like the checkpoint proxy rather than capping the body.
		if ct := resp.Header.Get("Content-Type"); ct != "" {
			w.Header().Set("Content-Type", ct)
		}
		w.WriteHeader(http.StatusOK)
		_, _ = io.Copy(w, resp.Body)
		return
	}
	if resp.StatusCode >= 400 && resp.StatusCode < 500 {
		// Version-agnostic gate (the skills proxy's pattern): the upstream's
		// own 4xx is passed through verbatim, so callers can distinguish
		// "chat not found" from "this worker build predates the route"
		// (GET /chats/{id}/status exists only on QwenPaw 2.2.1+; on older
		// builds the upstream's 404 is returned as-is and clients hide the
		// status indicator).
		if ct := resp.Header.Get("Content-Type"); ct != "" {
			w.Header().Set("Content-Type", ct)
		}
		w.WriteHeader(resp.StatusCode)
		_, _ = io.Copy(w, resp.Body)
		return
	}

	// Upstream 5xx — wrap it; the raw body is surfaced truncated.
	body, _ := io.ReadAll(io.LimitReader(resp.Body, chatErrorBodyCap))
	httputil.WriteError(w, http.StatusBadGateway, "worker upstream error: "+strings.TrimSpace(string(body)))
}

// listChats handles GET /api/v1/workers/{name}/chats.
// Forwards the whitelisted read-only filters to GET /api/chats.
func (h *ChatsHandler) listChats(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	for key := range q {
		if !chatListQueryWhitelist[key] {
			httputil.WriteError(w, http.StatusBadRequest, "unsupported query parameter: "+key)
			return
		}
	}
	upstreamPath := "/api/chats"
	if encoded := q.Encode(); encoded != "" {
		upstreamPath += "?" + encoded
	}
	h.proxy(w, r, r.PathValue("name"), upstreamPath)
}

// getChat handles GET /api/v1/workers/{name}/chats/{chat_id}.
// Forwards to GET /api/chats/{chat_id} (the full message transcript).
func (h *ChatsHandler) getChat(w http.ResponseWriter, r *http.Request) {
	chatID := r.PathValue("chat_id")
	if !chatIDPattern.MatchString(chatID) {
		httputil.WriteError(w, http.StatusBadRequest, "invalid chat id")
		return
	}
	if r.URL.RawQuery != "" {
		httputil.WriteError(w, http.StatusBadRequest, "unsupported query parameter")
		return
	}
	h.proxy(w, r, r.PathValue("name"), "/api/chats/"+chatID)
}

// getChatStatus handles GET /api/v1/workers/{name}/chats/{chat_id}/status.
// Forwards to GET /api/chats/{chat_id}/status (QwenPaw 2.2.1+; older
// builds return their own 404, passed through verbatim).
func (h *ChatsHandler) getChatStatus(w http.ResponseWriter, r *http.Request) {
	chatID := r.PathValue("chat_id")
	if !chatIDPattern.MatchString(chatID) {
		httputil.WriteError(w, http.StatusBadRequest, "invalid chat id")
		return
	}
	if r.URL.RawQuery != "" {
		httputil.WriteError(w, http.StatusBadRequest, "unsupported query parameter")
		return
	}
	h.proxy(w, r, r.PathValue("name"), "/api/chats/"+chatID+"/status")
}
