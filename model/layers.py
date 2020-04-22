import tensorflow as tf

from model.transformer_utils import positional_encoding, scaled_dot_product_attention


class PointWiseFFN(tf.keras.layers.Layer):
    
    def __init__(self, model_dim: int, dense_hidden_units: int, **kwargs):
        super(PointWiseFFN, self).__init__(**kwargs)
        self.d1 = tf.keras.layers.Dense(dense_hidden_units,
                                        activation='relu')  # (batch_size, seq_len, dense_hidden_units)
        self.d2 = tf.keras.layers.Dense(model_dim)  # (batch_size, seq_len, model_dim)
    
    def call(self, x):
        x = self.d1(x)
        x = self.d2(x)
        return x


class DecoderPrenet(tf.keras.layers.Layer):
    
    def __init__(self, model_dim: int, dense_hidden_units: int, dropout_rate: float = 0.5, **kwargs):
        super(DecoderPrenet, self).__init__(**kwargs)
        self.d1 = tf.keras.layers.Dense(dense_hidden_units,
                                        activation='relu')  # (batch_size, seq_len, dense_hidden_units)
        self.d2 = tf.keras.layers.Dense(model_dim, activation='relu')  # (batch_size, seq_len, model_dim)
        self.dropout_1 = tf.keras.layers.Dropout(dropout_rate)
        self.dropout_2 = tf.keras.layers.Dropout(dropout_rate)
    
    def call(self, x, dropout_rate: float = 0.5):
        self.dropout_1.dropout_rate = dropout_rate
        self.dropout_2.dropout_rate = dropout_rate
        x = self.d1(x)
        # use dropout also in inference for additional noise as suggested in the original tacotron2 paper
        x = self.dropout_1(x, training=True)
        x = self.d2(x)
        x = self.dropout_2(x, training=True)
        return x


class MultiHeadAttention(tf.keras.layers.Layer):
    
    def __init__(self, model_dim: int, num_heads: int, **kwargs):
        super(MultiHeadAttention, self).__init__(**kwargs)
        self.num_heads = num_heads
        self.model_dim = model_dim
        self.head_drop = HeadDrop()
        
        assert model_dim % self.num_heads == 0
        
        self.depth = model_dim // self.num_heads
        
        self.wq = tf.keras.layers.Dense(model_dim)
        self.wk = tf.keras.layers.Dense(model_dim)
        self.wv = tf.keras.layers.Dense(model_dim)
        
        self.dense = tf.keras.layers.Dense(model_dim)
    
    def split_heads(self, x, batch_size: int):
        """Split the last dimension into (num_heads, depth).
        Transpose the result such that the shape is (batch_size, num_heads, seq_len, depth)
        """
        
        x = tf.reshape(x, (batch_size, -1, self.num_heads, self.depth))
        return tf.transpose(x, perm=[0, 2, 1, 3])
    
    def call(self, v, k, q_in, mask, training, drop_n_heads):
        batch_size = tf.shape(q_in)[0]
        
        q = self.wq(q_in)  # (batch_size, seq_len, model_dim)
        k = self.wk(k)  # (batch_size, seq_len, model_dim)
        v = self.wv(v)  # (batch_size, seq_len, model_dim)
        
        q = self.split_heads(q, batch_size)  # (batch_size, num_heads, seq_len_q, depth)
        k = self.split_heads(k, batch_size)  # (batch_size, num_heads, seq_len_k, depth)
        v = self.split_heads(v, batch_size)  # (batch_size, num_heads, seq_len_v, depth)
        
        scaled_attention, attention_weights = scaled_dot_product_attention(q, k, v, mask)
        scaled_attention = self.head_drop(scaled_attention, training=training, drop_n_heads=drop_n_heads)
        
        scaled_attention = tf.transpose(scaled_attention,
                                        perm=[0, 2, 1, 3])  # (batch_size, seq_len_q, num_heads, depth)
        concat_attention = tf.reshape(scaled_attention,
                                      (batch_size, -1, self.model_dim))  # (batch_size, seq_len_q, model_dim)
        concat_query = tf.concat([q_in, concat_attention], axis=-1)
        output = self.dense(concat_query)  # (batch_size, seq_len_q, model_dim)
        return output, attention_weights


class SelfAttentionResNorm(tf.keras.layers.Layer):
    
    def __init__(self, model_dim: int, num_heads: int, dropout_rate: float, **kwargs):
        super(SelfAttentionResNorm, self).__init__(**kwargs)
        self.mha = MultiHeadAttention(model_dim, num_heads)
        self.ln = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.dropout = tf.keras.layers.Dropout(dropout_rate)
    
    def call(self, x, training, mask, drop_n_heads):
        attn_out, attn_weights = self.mha(x, x, x, mask, training=training,
                                          drop_n_heads=drop_n_heads)  # (batch_size, input_seq_len, model_dim)
        attn_out = self.dropout(attn_out, training=training)
        out = self.ln(x + attn_out)  # (batch_size, input_seq_len, model_dim)
        return out, attn_weights


class FFNResNorm(tf.keras.layers.Layer):
    
    def __init__(self, model_dim: int, dense_hidden_units: int, dropout_rate: float = 0.1, **kwargs):
        super(FFNResNorm, self).__init__(**kwargs)
        self.ffn = PointWiseFFN(model_dim, dense_hidden_units)
        self.dropout = tf.keras.layers.Dropout(dropout_rate)
        self.ln = tf.keras.layers.LayerNormalization(epsilon=1e-6)
    
    def call(self, x, training):
        ffn_out = self.ffn(x)  # (batch_size, input_seq_len, model_dim)
        ffn_out = self.dropout(ffn_out, training=training)
        out = self.ln(x + ffn_out)  # (batch_size, input_seq_len, model_dim)
        
        return out


class HeadDrop(tf.keras.layers.Layer):
    """ Randomly drop n heads. """
    
    def __init__(self, **kwargs):
        super(HeadDrop, self).__init__(**kwargs)
    
    def call(self, batch, training: bool, drop_n_heads: int):
        if not training or (drop_n_heads == 0):
            return batch
        if len(tf.shape(batch)) != 4:
            raise Exception('attention values must be 4 dimensional')
        batch_size = tf.shape(batch)[0]
        head_n = tf.shape(batch)[1]
        if head_n == 1:
            return batch
        # assert drop_n_heads < head_n, 'drop_n_heads must less than number of heads'
        keep_head_mask = tf.concat([tf.ones(head_n - drop_n_heads), tf.zeros(drop_n_heads)], axis=0)
        keep_head_mask = tf.tile(keep_head_mask[tf.newaxis], [batch_size, 1])
        keep_head_mask = tf.map_fn(tf.random.shuffle, keep_head_mask, dtype=batch.dtype)
        keep_head_mask = keep_head_mask[:, :, tf.newaxis, tf.newaxis]
        return batch * keep_head_mask * tf.cast(head_n / (head_n - drop_n_heads), tf.float32)


# class HeadDrop(tf.keras.layers.Layer):
#     """ Randomly drop whole heads. """
#
#     def __init__(self, rate=0.3, **kwargs):
#         super(HeadDrop, self).__init__(**kwargs)
#         self.rate = rate
#
#     def call(self, batch, training):
#         if not training:
#             return batch
#         if len(tf.shape(batch)) != 4:  # single head
#             raise Exception('Attention values should be 4 dimensional')
#         batch_size = tf.shape(batch)[0]
#         head_n = tf.shape(batch)[1]
#         if head_n == 1:
#             return batch
#         return tf.nn.dropout(batch, rate=self.rate, noise_shape=(batch_size, head_n, 1, 1))

class EncoderLayer(tf.keras.layers.Layer):
    
    def __init__(self, model_dim: int, num_heads: int, dense_hidden_units: int, dropout_rate: float = 0.1, **kwargs):
        super(EncoderLayer, self).__init__(**kwargs)
        self.sarn = SelfAttentionResNorm(model_dim, num_heads, dropout_rate=dropout_rate)
        self.ffn = FFNResNorm(model_dim, dense_hidden_units)
    
    def call(self, x, training, mask, drop_n_heads):
        attn_out, _ = self.sarn(x, mask=mask, training=training, drop_n_heads=drop_n_heads)
        return self.ffn(attn_out, training=training)


class Encoder(tf.keras.layers.Layer):
    
    def __init__(self, model_dim: int, num_heads: list, dense_hidden_units: int,
                 maximum_position_encoding: int,
                 dropout_rate: float = 0.1, **kwargs):
        super(Encoder, self).__init__(**kwargs)
        self.model_dim = model_dim
        self.pos_encoding = positional_encoding(maximum_position_encoding, model_dim)
        self.enc_layers = [EncoderLayer(model_dim, heads, dense_hidden_units, dropout_rate) for heads in num_heads]
        self.dropout = tf.keras.layers.Dropout(dropout_rate)
    
    def call(self, inputs, training, mask, drop_n_heads):
        seq_len = tf.shape(inputs)[1]
        x = inputs * tf.math.sqrt(tf.cast(self.model_dim, tf.float32))
        x += self.pos_encoding[:, :seq_len, :]
        x = self.dropout(x, training=training)
        for layer in self.enc_layers:
            x = layer(x, training, mask, drop_n_heads=drop_n_heads)
        return x


class CrossAttentionResnorm(tf.keras.layers.Layer):
    
    def __init__(self, model_dim: int, num_heads: int, dropout_rate: float = 0.1, **kwargs):
        super(CrossAttentionResnorm, self).__init__(**kwargs)
        self.mha = MultiHeadAttention(model_dim, num_heads)
        self.layernorm = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.dropout = tf.keras.layers.Dropout(dropout_rate)
    
    def call(self, q, k, v, training, mask, drop_n_heads):
        attn_values, attn_weights = self.mha(v, k=k, q_in=q, mask=mask, training=training, drop_n_heads=drop_n_heads)
        attn_values = self.dropout(attn_values, training=training)
        out = self.layernorm(attn_values + q)
        return out, attn_weights


class DecoderLayer(tf.keras.layers.Layer):
    
    def __init__(self, model_dim: int, num_heads: int, dense_hidden_units: int, dropout_rate: float = 0.1, **kwargs):
        super(DecoderLayer, self).__init__(**kwargs)
        self.sarn = SelfAttentionResNorm(model_dim, num_heads, dropout_rate=dropout_rate)
        self.carn = CrossAttentionResnorm(model_dim, num_heads, dropout_rate=dropout_rate)
        self.ffn = FFNResNorm(model_dim, dense_hidden_units, dropout_rate=dropout_rate)
    
    def call(self, x, enc_output, training, look_ahead_mask, padding_mask, drop_n_heads):
        attn1, attn_weights_block1 = self.sarn(x, mask=look_ahead_mask, training=training, drop_n_heads=drop_n_heads)
        
        attn2, attn_weights_block2 = self.carn(attn1, v=enc_output, k=enc_output,
                                               mask=padding_mask, training=training, drop_n_heads=drop_n_heads)
        ffn_out = self.ffn(attn2, training=training)
        return ffn_out, attn_weights_block1, attn_weights_block2


class Decoder(tf.keras.layers.Layer):
    
    def __init__(self, model_dim: int, num_heads: list, dense_hidden_units: int,
                 maximum_position_encoding: int,
                 dropout_rate: float = 0.1, **kwargs):
        super(Decoder, self).__init__(**kwargs)
        self.model_dim = model_dim
        self.pos_encoding = positional_encoding(maximum_position_encoding, model_dim)
        self.dec_layers = [DecoderLayer(model_dim, heads, dense_hidden_units, dropout_rate) for heads in num_heads]
        self.dropout = tf.keras.layers.Dropout(dropout_rate)
    
    def call(self, inputs, enc_output, training, look_ahead_mask, padding_mask, drop_n_heads):
        seq_len = tf.shape(inputs)[1]
        attention_weights = {}
        x = inputs * tf.math.sqrt(tf.cast(self.model_dim, tf.float32))
        x += self.pos_encoding[:, :seq_len, :]
        x = self.dropout(x, training=training)
        for i, layer in enumerate(self.dec_layers):
            x, block1, block2 = layer(x, enc_output, training, look_ahead_mask, padding_mask, drop_n_heads)
            
            attention_weights[f'decoder_layer{i + 1}_block1'] = block1
            attention_weights[f'decoder_layer{i + 1}_block2'] = block2
        
        return x, attention_weights


class Postnet(tf.keras.layers.Layer):
    
    def __init__(self, mel_channels: int, conv_filters: int = 256, conv_layers: int = 5, kernel_size: int = 5,
                 **kwargs):
        super(Postnet, self).__init__(**kwargs)
        self.mel_channels = mel_channels
        self.stop_linear = tf.keras.layers.Dense(3)
        self.postnet_conv_layers = PostnetConvLayers(
            out_size=mel_channels, n_filters=conv_filters, n_layers=conv_layers, kernel_size=kernel_size
        )
        self.add_layer = tf.keras.layers.Add()
    
    def call(self, x, training):
        stop = self.stop_linear(x)
        conv_out = self.conv_net(x, training=training)
        return {
            'mel_linear': x,
            'final_output': conv_out,
            'stop_prob': stop,
        }
    
    def conv_net(self, x, *, training):
        conv_out = self.postnet_conv_layers(x, training)
        x = self.add_layer([conv_out, x])
        return x


class PostnetConvLayers(tf.keras.layers.Layer):
    
    def __init__(self, out_size: int, n_filters: int = 256, n_layers: int = 5, kernel_size: int = 5,
                 dropout_prob: float = 0.5, **kwargs):
        super(PostnetConvLayers, self).__init__(**kwargs)
        self.convolutions = [tf.keras.layers.Conv1D(filters=n_filters,
                                                    kernel_size=kernel_size,
                                                    padding='causal',
                                                    activation='tanh')
                             for _ in range(n_layers - 1)]
        self.dropouts = [tf.keras.layers.Dropout(dropout_prob) for _ in range(n_layers - 1)]
        self.last_conv = tf.keras.layers.Conv1D(filters=out_size,
                                                kernel_size=kernel_size,
                                                padding='causal',
                                                activation='linear')
        self.batch_norms = [tf.keras.layers.BatchNormalization() for _ in range(n_layers)]
    
    def call(self, x, training):
        for i in range(0, len(self.convolutions)):
            x = self.convolutions[i](x)
            x = self.batch_norms[i](x, training=training)
            x = self.dropouts[i](x, training=training)
        x = self.last_conv(x)
        x = self.batch_norms[-1](x, training=training)
        return x
