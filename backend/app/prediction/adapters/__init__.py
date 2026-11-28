"""Reviewed model adapters (#127): the allowlisted package `ArtifactLoader` loads model code
from. One adapter serves every version of a model family. Each defines
`build(card, weights) -> Predictor`, checks the weights with `verified_weights` before
parsing them, and never unpickles."""
