# Labeler trials: neural nets against the trees

Run again at ~200 reviewed songs (2026-10-20, 114 songs: the trees stay; a blend gained ~1 point, near noise).

    .venv\Scripts\python tools\labeler_trials\nn_export.py               the data, 10 folds by song, the trees' predictions
    .venv-songformer\Scripts\python tools\labeler_trials\nn_train.py     a bidirectional GRU from scratch (HID, EPOCHS)
    .venv-songformer\Scripts\python tools\labeler_trials\sf_finetune.py  SongFormer's own layers fine-tuned (EPOCHS, LR, FREEZE)

At 114 songs (types / drop-not-drop, 10 folds): the trees 83.9% / 91.4%; the GRU 81.4% / 91.2%; SongFormer
fine-tuned (8 epochs, lr 3e-5) 74.2% / 90.8% alone, 84.2% / 92.0% as 30% of a blend with the trees (on the 65
songs reviewed 2026-10-19: 83.1% / 91.7% against the trees' 82.1% / 90.7%). Wire the blend in if its lead grows
to 2-3 points.

At 129 of the user's songs (2026-10-21, as sections; ADD_CLAUDE=1 adds Claude's 188 reviews to training, the
scores are on the user's songs alone): the trees 84.7% / 91.9%. The GRU 81.7% / 90.5%, with Claude's 82.7% / 91.7%.
SongFormer fine-tuned 77.1% / 91.6%, with Claude's 77.6% / 91.6%. The best blend (0.3 SongFormer with Claude's +
0.7 trees) 84.4% / 91.9%. Claude's reviews help the nets by ~0.5-1 point but none catches the trees: the trees stay.
