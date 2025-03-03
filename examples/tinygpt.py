#!/usr/bin/env python
import os
import argparse
import urllib.request
import numpy as np
import tinygrad.nn as nn
from tinygrad.nn import optim
from tinygrad.state import get_parameters
from tinygrad.tensor import Tensor, dtypes

# tiny shakespear input text
input_url = "https://raw.githubusercontent.com/karpathy/char-rnn/master/data/tinyshakespeare/input.txt"

Tensor.manual_seed(31337)

# hyperparameters
batch_size = 64 # how many independent sequences will we process in parallel?
block_size = 128 # what is the maximum context length for predictions?
max_iters = 5000
eval_interval = 500
learning_rate = 3e-4
eval_iters = 200
n_embd = 192
n_head = 4
n_layer = 4
dropout = 0.2

BOLD = '\033[1m'
END = '\033[0m'

def get_input_text(url, file_path, clean):
  """ fetch and return tiny Shakespeare input text """
  if not os.path.exists(file_path):
    urllib.request.urlretrieve(url, file_path)
    print("stored tiny shakespeare to " + BOLD + file_path + END)
  else:
    print("reading shakespeare from " + BOLD + file_path + END)
  with open(file_path, 'r') as f:
    text = f.read()
  if clean:
    os.remove(file_path)
  return text

def cross_entropy(out, Y):
  """ negative Log Loss function """
  num_classes = out.shape[-1]
  YY = Y.numpy().flatten().astype(np.int32)
  y = np.zeros((YY.shape[0], num_classes), np.float32)
  y[range(y.shape[0]),YY] = -1.0*num_classes
  y = y.reshape(list(Y.shape)+[num_classes])
  y = Tensor(y)
  return out.mul(y).mean()

def estimate_loss():
  out = {}
  Tensor.training = False
  for split in ['train', 'val']:
    losses = np.zeros(eval_iters)
    for k in range(eval_iters):
      X, Y = get_batch(split)
      _, loss = model(X, Y)
      losses[k] = loss.numpy()
    out[split] = losses.mean()
  Tensor.training = True
  return out

# data loading
def get_batch(split):
  # generate a small batch of data of inputs x and targets y
  data = train_data if split == "train" else val_data
  ix = Tensor.uniform(batch_size, low=0, high=(data.shape[0] - block_size)).cast(dtype=dtypes.int32)
  x = Tensor.stack([data[i:i+block_size] for i in ix.numpy()])
  y = Tensor.stack([data[i+1:i+block_size+1] for i in ix.numpy()])
  return x, y


class Head():
  """ one head of self-attention """

  def __init__(self, head_size):
    # optimized k,q,v layer to compute with a single linear transformation
    self.to_kqv = nn.Linear(n_embd, 3 * head_size, bias=False)

  def __call__(self, x):
    B, T, _ = x.shape
    kqv = self.to_kqv(x) # shape: (B, T, 3 * head_size)
    kqv = kqv.reshape(B, T, 3, -1) # shape: (B, T, 3, head_size)

    # splitting k,q,v
    k = kqv[:,:,0,:]
    q = kqv[:,:,1,:]
    v = kqv[:,:,2,:]

    # compute attention scores ("affinities")
    wei = q @ Tensor.transpose(k, -2, -1) * n_embd**-0.5
    # equal to a lower triangular matrix (tril) masked_fill in pytorch
    mask = Tensor(np.triu(np.ones((T,T), dtype=np.float32) * -np.inf, k=1))
    wei = wei + mask
    wei = wei.softmax(-1)
    wei = wei.dropout(dropout)

    # perform the weighted aggregation of the values
    out = wei @ v  # (B, T, T) @ (B, T, hs) -> (B, T, hs)
    return out


class MultiHeadAttention():

  """ multiple heads of self-attention in parallel """
  def __init__(self, num_heads, head_size):
    self.heads = [Head(head_size) for _ in range(num_heads)]
    self.proj = nn.Linear(n_embd, n_embd)

  def __call__(self, x):
    out = self.heads[0](x).cat(*[h(x) for h in self.heads[1:]], dim=-1)
    out = self.proj(out).dropout(dropout)
    return out


class FeedForward():
  """ a simple linear layer followed by a non-linearity """

  def __init__(self, n_embd):
    self.net = [
      nn.Linear(n_embd, 4 * n_embd),
      Tensor.relu,
      nn.Linear(4 * n_embd, n_embd)
    ]

  def __call__(self, x):
    return x.sequential(self.net).dropout(dropout)


class Block():
  """ transformer block: communication followed by computation """
  def __init__(self, n_embd, n_head):
    # n_embd: embedding dimension, n_head: the number of heads we'd like
    head_size = n_embd // n_head
    self.sa = MultiHeadAttention(n_head, head_size)
    self.ffwd = FeedForward(n_embd)
    self.ln1 = nn.LayerNorm(n_embd)
    self.ln2 = nn.LayerNorm(n_embd)

  def __call__(self, x):
    x = x + self.sa(self.ln1(x))
    x = x + self.ffwd(self.ln2(x))
    return x


class GPTLanguageModel():
  """ a decoder only transformer """

  def __init__(self, vocab_size):
    # each token directly reads off the logits for the next token from a lookup table
    self.token_embedding_table = nn.Embedding(vocab_size, n_embd)
    self.position_embedding_table = nn.Embedding(block_size, n_embd)
    self.blocks = [Block(n_embd, n_head) for _ in range(n_layer)]
    self.ln_f = nn.LayerNorm(n_embd) # final layer norm
    self.lm_head = nn.Linear(n_embd, vocab_size)

  def __call__(self, idx, targets=None):
    _, T = idx.shape

    # idx and targets are both (B,T) tensor of integers
    tok_emb = self.token_embedding_table(idx) # (B,T,C)
    pos_emb = self.position_embedding_table(Tensor.arange(T, dtype=dtypes.int8).reshape(1,T)) # (T,C)
    x = tok_emb + pos_emb # (B,T,C)
    for block in self.blocks: x = block(x) # (B,T,C)
    x = self.ln_f(x) # (B,T,C)
    logits = self.lm_head(x) # (B,T,vocab_size)

    if targets is None:
      loss = None
    else:
      # log softmax to get predictions for cross_entropy loss calculation
      predictions = logits.log_softmax(-1)
      loss = cross_entropy(predictions, targets)
    return logits, loss

  def generate(self, idx, max_new_tokens):
    out = '' # full generated output string
    # idx is (B, T) array of indices in the current context
    for _ in range(max_new_tokens):
      # crop idx to the last block_size tokens
      idx_cond = idx[:, -block_size:]
      # get the predictions
      logits, _ = self(idx_cond)
      # focus only on the last time step
      logits = logits[:, -1, :] # becomes (B, C)
      # apply softmax to get probabilities
      probs = logits.softmax(-1) # (B, C)
      # sample from the distribution
      idx_next = [np.random.choice(len(p), size=1, p=p) for p in probs.numpy()]
      token = decode(idx_next[0])
      # print and flush for running stream of next token
      print(token, flush=True, end='')
      out = out + token
      # append sampled index to the running sequence
      idx = Tensor.cat(idx, Tensor(idx_next), dim=1) # (B, T+1)
      # limit idx to block_size to reduce context length for efficiency but worse output
      # comment this line out to use full concatenated idx context for more accuracy ** very slow **
      idx = idx[:,-block_size:]
    return out


if __name__ == "__main__":
  """
  Generative Transformer implementation Pre-trained on tiny Shakespeare data set.
  This is almost a direct copy of the video how-to by Andrej Karpathy implemented in tinygrad.
  Reference of the pytorch implementation and video.
  
      YouTube : https://youtu.be/kCc8FmEb1nY
      Github  : https://github.com/karpathy/ng-video-lecture/tree/master
  """

  parser = argparse.ArgumentParser(description="""Tiny Shakespeare GPT""")
  parser.add_argument('--input', default=os.path.join(os.path.sep, 'tmp', 'input.txt'),
                      help="Where to save the input text, defaults to '/tmp/input.txt'",
                      metavar="PATH")
  parser.add_argument('--output', default=None,
                      help='Save the output text, defaults to NO OUTPUT.',
                      metavar="PATH")
  parser.add_argument('--clean', action="store_true",
                      help='Delete the input text file after run, defaults to False.')
  args = parser.parse_args()

  text = get_input_text(input_url, args.input, args.clean)

  # here are all the unique characters that occur in this text
  chars = sorted(list(set(text)))
  vocab_size = len(chars)
  # create a mapping from characters to integers
  stoi = { ch:i for i,ch in enumerate(chars) }
  itos = { i:ch for i,ch in enumerate(chars) }

  # encoder: take a string, output a list of integers
  encode = lambda s: [stoi[c] for c in s]  # noqa: E731
  # decoder: take a list of integers, output a string
  decode = lambda l: ''.join([itos[i] for i in l])  # noqa: E731

  # train and test splits
  data = Tensor(encode(text), dtype=dtypes.int64, requires_grad=False)
  n = int(0.9*data.shape[0])
  train_data = data[:n]
  val_data = data[n:]

  model = GPTLanguageModel(vocab_size)
  parameters = get_parameters(model)
  # print the number of parameters in the model
  print(sum(p.numel() for p in parameters)/1e6, 'M parameters')

  # create a tinygrad AdamW optimizer
  optimizer = optim.AdamW(parameters, lr=learning_rate)

  Tensor.training = True
  print("imbuing...")
  for iter in range(max_iters):

    # every once in a while evaluate the loss on train and val sets
    if iter % eval_interval == 0 or iter == max_iters - 1:
      losses = estimate_loss()
      print(f"step {iter}: train loss {losses['train']:.4f}, val loss {losses['val']:.4f}")

    # sample a batch of data
    xb, yb = get_batch("train")

    # evaluate the loss
    _, loss = model(xb, yb)
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()

  Tensor.training = False
  print("-- hark! --")
  # generate output tokens
  context = Tensor.zeros((1, 1), dtypes.int64)
  out = model.generate(context, max_new_tokens=1000)
  print("\n-- exeunt. --")
  if (args.output is not None):
    with open(args.output, 'w') as f:
      print(out, file=f)
    print("output saved to " + BOLD + args.output + END)