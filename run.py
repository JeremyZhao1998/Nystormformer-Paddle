import paddle
from paddle.io import Dataset, DataLoader

from transformers import AutoTokenizer
from datasets import load_dataset

from nystromformer_paddle.nystromformer_config import NystromformerConfig
from nystromformer_paddle.nystromformer_paddle import NystromformerForSequenceClassification
from nystromformer_paddle.utils import update_metrics, get_f1_score

import pickle

import reprod_log

dataset = 'imdb'
max_len = 512
batch_size = 2
device = 'cpu'
lr = 3e-5
epochs = 4
mixed_precision = True


def prepare_loader(split):
    data_path = 'data/tokenized_' + dataset + '_' + split + '_' + str(max_len) + '.pkl'
    try:
        with open(data_path, 'rb') as f:
            input_ids, token_type_ids, attention_mask, labels = pickle.load(f)
    except FileNotFoundError:
        raw_data = load_dataset(dataset)
        tokenizer = AutoTokenizer.from_pretrained('pretrained_files')
        tokenized_data = tokenizer(
            raw_data[split]['text'],
            truncation=True, padding='max_length', max_length=max_len - 2,
            return_tensors='np'
        )
        f = open(data_path, 'wb')
        input_ids, token_type_ids, attention_mask = \
            tokenized_data['input_ids'], tokenized_data['token_type_ids'], tokenized_data['attention_mask']
        labels = raw_data[split]['label']
        pickle.dump((input_ids, token_type_ids, attention_mask, labels), f)

    class TextDataset(Dataset):
        def __len__(self):
            return len(input_ids)

        def __getitem__(self, idx):
            return input_ids[idx], token_type_ids[idx], attention_mask[idx], labels[idx]

    loader = DataLoader(dataset=TextDataset(), batch_size=batch_size, shuffle=split == 'train')
    return loader


def main():
    paddle.device.set_device(device)
    train_loader = prepare_loader('train')
    valid_loader = prepare_loader('test')

    model_config = NystromformerConfig()
    model_config.load_config_json('pretrained_files/config.json')
    model = NystromformerForSequenceClassification(model_config)
    model.nystromformer.load_dict(paddle.load('pretrained_files/nystromformer_model.params'))

    optimizer = paddle.optimizer.AdamW(
        parameters=model.parameters(),
        learning_rate=lr,
        beta1=0.9, beta2=0.999, epsilon=1e-6, weight_decay=0.01
    )

    amp_scaler = paddle.amp.GradScaler() if mixed_precision and device != 'cpu' else None

    log = reprod_log.ReprodLogger()

    for epoch in range(epochs):
        precision, recall = paddle.metric.Precision(), paddle.metric.Recall()
        precision.reset()
        recall.reset()
        model.train()
        for batch_data in train_loader:
            outputs = model(
                input_ids=batch_data[0],
                token_type_ids=batch_data[1],
                attention_mask=batch_data[2],
                labels=batch_data[3]
            )
            logits, loss = outputs['logits'], outputs['loss']
            update_metrics(logits, batch_data[3], [precision, recall])
            if amp_scaler is not None:
                scaled = amp_scaler.scale(loss)
                scaled.backward()
                amp_scaler.minimize(optimizer, scaled)
            else:
                loss.backward()
                optimizer.minimize(loss)
            optimizer.clear_gradients()
            print('epoch:', epoch, 'loss:', loss.numpy())
        train_f1_score = get_f1_score(precision, recall)

        precision.reset()
        recall.reset()
        model.eval()
        for batch_data in valid_loader:
            outputs = model(
                input_ids=batch_data[0],
                token_type_ids=batch_data[1],
                attention_mask=batch_data[2],
                labels=batch_data[3]
            )
            logits, loss = outputs['logits'], outputs['loss']
            update_metrics(logits, batch_data[3], [precision, recall])
        valid_f1_score = get_f1_score(precision, recall)
        print('----------------------------------------------')
        print('epoch:', epoch, 'finished. train_f1_score:', train_f1_score, 'valid_f1_score:', valid_f1_score)
        print('----------------------------------------------')
        log.add('epoch' + str(epoch), valid_f1_score)
    log.save('fine_tune_log.npy')


if __name__ == '__main__':
    main()
