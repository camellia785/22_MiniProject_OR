import math

import torch
import torch.nn as nn

from rl4co.models.co.am.utils import get_log_likelihood
from rl4co.models.co.ptrnet.encoder import Encoder
from rl4co.models.co.ptrnet.decoder import Decoder


class PointerNetworkPolicy(nn.Module):
    def __init__(
        self,
        env,
        embedding_dim: int = 128,
        hidden_dim: int = 128,
        tanh_clipping=10.0,
        mask_inner=True,
        mask_logits=True,
        **kwargs
    ):
        super(PointerNetworkPolicy, self).__init__()

        self.env = env
        assert self.env.name == "tsp", "Only the Euclidean TSP env supported"

        self.input_dim = 2

        self.encoder = Encoder(embedding_dim, hidden_dim)

        self.decoder = Decoder(
            embedding_dim,
            hidden_dim,
            tanh_exploration=tanh_clipping,
            use_tanh=tanh_clipping > 0,
            num_glimpses=1,
            mask_glimpses=mask_inner,
            mask_logits=mask_logits,
        )

        # Trainable initial hidden states
        std = 1.0 / math.sqrt(embedding_dim)
        self.decoder_in_0 = nn.Parameter(torch.FloatTensor(embedding_dim))
        self.decoder_in_0.data.uniform_(-std, std)

        self.embedding = nn.Parameter(torch.FloatTensor(self.input_dim, embedding_dim))
        self.embedding.data.uniform_(-std, std)

    def forward(
        self, td, phase: str = "train", decode_type="sampling", eval_tours=None
    ):
        batch_size, graph_size, input_dim = td["observation"].size()

        embedded_inputs = torch.mm(
            td["observation"].transpose(0, 1).contiguous().view(-1, input_dim),
            self.embedding,
        ).view(graph_size, batch_size, -1)

        # query the actor net for the input indices
        # making up the output, and the pointer attn
        _log_p, actions = self._inner(embedded_inputs, decode_type, eval_tours)

        reward = self.env.get_reward(td["observation"], actions)

        # Log likelyhood is calculated within the model since returning it per action does not work well with
        # DataParallel since sequences can be of different lengths
        ll = get_log_likelihood(_log_p, actions, td.get("mask", None))

        out = {"reward": reward, "log_likelihood": ll, "actions": actions}
        return out

    def _inner(self, inputs, decode_type="sampling", eval_tours=None):
        encoder_hx = encoder_cx = torch.zeros(
            1, *inputs.shape[1:], device=inputs.device
        )  # (1, inputs.size(1), self.encoder.hidden_dim, device=inputs.device, out=inputs.data.new(), requires_grad=False)

        # encoder forward pass
        enc_h, (enc_h_t, enc_c_t) = self.encoder(inputs, (encoder_hx, encoder_cx))

        dec_init_state = (enc_h_t[-1], enc_c_t[-1])

        # repeat decoder_in_0 across batch
        decoder_input = self.decoder_in_0.unsqueeze(0).repeat(inputs.size(1), 1)

        (pointer_probs, input_idxs), dec_hidden_t = self.decoder(
            decoder_input, inputs, dec_init_state, enc_h, decode_type, eval_tours
        )

        return pointer_probs, input_idxs