package server

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"

	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/gateway"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/httputil"
)

// GatewayHandler handles /api/v1/gateway/* requests using the unified gateway.Client.
type GatewayHandler struct {
	gw gateway.Client
}

func NewGatewayHandler(gw gateway.Client) *GatewayHandler {
	return &GatewayHandler{gw: gw}
}

func (h *GatewayHandler) CreateConsumer(w http.ResponseWriter, r *http.Request) {
	if h.gw == nil {
		httputil.WriteError(w, http.StatusNotImplemented, "no gateway backend available")
		return
	}

	var req CreateConsumerRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		httputil.WriteError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if req.Name == "" {
		httputil.WriteError(w, http.StatusBadRequest, "name is required")
		return
	}

	result, err := h.gw.EnsureConsumer(r.Context(), gateway.ConsumerRequest{
		Name:          req.Name,
		CredentialKey: req.CredentialKey,
	})
	if err != nil {
		log.Printf("[ERROR] create consumer %s: %v", req.Name, err)
		httputil.WriteError(w, http.StatusInternalServerError, err.Error())
		return
	}

	httputil.WriteJSON(w, http.StatusCreated, ConsumerResponse{
		Name:       req.Name,
		ConsumerID: result.ConsumerID,
		APIKey:     result.APIKey,
		Status:     result.Status,
	})
}

func (h *GatewayHandler) BindConsumer(w http.ResponseWriter, r *http.Request) {
	if h.gw == nil {
		httputil.WriteError(w, http.StatusNotImplemented, "no gateway backend available")
		return
	}

	consumerName := r.PathValue("id")
	if consumerName == "" {
		httputil.WriteError(w, http.StatusBadRequest, "consumer name is required")
		return
	}

	if err := h.gw.AuthorizeAIRoutes(r.Context(), consumerName, ""); err != nil {
		log.Printf("[ERROR] bind consumer %s: %v", consumerName, err)
		httputil.WriteError(w, http.StatusInternalServerError, err.Error())
		return
	}

	w.WriteHeader(http.StatusNoContent)
}

func (h *GatewayHandler) DeleteConsumer(w http.ResponseWriter, r *http.Request) {
	if h.gw == nil {
		httputil.WriteError(w, http.StatusNotImplemented, "no gateway backend available")
		return
	}

	consumerName := r.PathValue("id")
	if consumerName == "" {
		httputil.WriteError(w, http.StatusBadRequest, "consumer name is required")
		return
	}

	if err := h.gw.DeleteConsumer(r.Context(), consumerName); err != nil {
		log.Printf("[ERROR] delete consumer %s: %v", consumerName, err)
		httputil.WriteError(w, http.StatusInternalServerError, err.Error())
		return
	}

	w.WriteHeader(http.StatusNoContent)
}

// ListModels serves GET /api/v1/models: the read-only model catalog. Each
// entry is an AI route — the route name is the model alias used in
// Worker/Manager model fields, upstreams are the providers serving it, and
// allowedConsumers are the consumers authorized on the route. The route is
// registered with the "gateway" resource kind, which the authorizer grants
// to admin/manager only (L1); team leaders, humans and workers get 403.
func (h *GatewayHandler) ListModels(w http.ResponseWriter, r *http.Request) {
	if h.gw == nil {
		httputil.WriteError(w, http.StatusNotImplemented, "no gateway backend available")
		return
	}

	routes, err := h.gw.ListAIRoutes(r.Context())
	if err != nil {
		if errors.Is(err, gateway.ErrUnsupportedOp) {
			httputil.WriteError(w, http.StatusNotImplemented, err.Error())
			return
		}
		log.Printf("[ERROR] list models: %v", err)
		httputil.WriteError(w, http.StatusBadGateway, err.Error())
		return
	}

	models := routes
	if models == nil {
		models = []gateway.AIRouteInfo{}
	}
	httputil.WriteJSON(w, http.StatusOK, ModelListResponse{Models: models, Total: len(models)})
}
