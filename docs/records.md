# 记录样式

以下为当前业务文件中的实际记录，字段口径见 `data-contract.md`；字段标识保持业务接口写法。

### `accounts`

字段：`account`、`acquired_at`、`cohort`。

```json
{
  "account": "a000",
  "cohort": "渠道1",
  "acquired_at": 1
}
```

### `spells`

字段：`account`、`end`、`known_at`、`spell`、`start`、`status`。

```json
{
  "spell": "a000-1",
  "account": "a000",
  "start": 1,
  "end": 5,
  "status": "ended",
  "known_at": 1
}
```

### `ledger`

字段：`account`、`amount`、`effective_at`、`entry`、`kind`、`known_at`。

```json
{
  "entry": "a000-1-c",
  "account": "a000",
  "kind": "charge",
  "amount": 40,
  "effective_at": 1,
  "known_at": 1
}
```
