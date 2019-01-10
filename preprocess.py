import os
import re
import sys
import traceback
import six
from multiprocessing import Pool
import itertools
import torch
from torch.utils import data
from collections import Counter
import sentencepiece as spm
import fire
import cate_db
import tqdm
import h5py
import json
from misc import get_logger, Option
opt = Option('./config.json')


class Reader(object):
    def __init__(self, data_path_list, div, begin_offset, end_offset, progress=False):
        self.div = div
        self.data_path_list = data_path_list
        self.begin_offset = begin_offset
        self.end_offset = end_offset
        self.progress = progress

    def is_range(self, i):
        if self.begin_offset is not None and i < self.begin_offset:
            return False
        if self.end_offset is not None and self.end_offset <= i:
            return False
        return True

    def get_size(self):
        offset = 0
        count = 0
        for data_path in self.data_path_list:
            h = h5py.File(data_path, 'r')
            sz = h[self.div]['pid'].shape[0]
            if not self.begin_offset and not self.end_offset:
                offset += sz
                count += sz
                continue
            if self.begin_offset and offset + sz < self.begin_offset:
                offset += sz
                continue
            if self.end_offset and self.end_offset < offset:
                break
            for i in range(sz):
                if not self.is_range(offset + i):
                    continue
                count += 1
            offset += sz
        return count

    def generate(self):
        offset = 0
        for h_index, data_path in enumerate(self.data_path_list):
            h = h5py.File(data_path, 'r')[self.div]
            sz = h['pid'].shape[0]
            if self.begin_offset and offset + sz < self.begin_offset:
                offset += sz
                continue
            if self.end_offset and self.end_offset < offset:
                break
            #for i in range(sz):
            if self.progress:
                iteration = tqdm.tqdm(range(sz), mininterval=1)
            else:
                iteration = range(sz)
            for i in iteration:
                if not self.is_range(offset + i):
                    continue
                #class_name = self.get_class(h, i)
                yield h['pid'][i], h['product'][i], h['img_feat'][i], \
                h['bcateid'][i], h['mcateid'][i], h['scateid'][i], h['dcateid'][i] 
        
            offset += sz
   
def preproc_titles(titles):
    titles = [' '.join(cate_db.re_sc.sub(' ', title).strip().split()) for title in titles]

def write_titles(titles, titles_path):
    titles_dir = os.path.dirname(titles_path)
    os.makedirs(titles_dir, exist_ok=True)
    
    f_titles = open(titles_path, 'w')
    #for i, (title) in enumerate(titles):
    for title in tqdm.tqdm(titles, mininterval=1):
        f_titles.write(title + '\n')
    

def train_spm(txt_path='data/vocab/train_titles.txt', spm_path='data/vocab/spm', 
              vocab_size=8000, input_sentence_size=10000000):
    spm_dir = os.path.dirname(spm_path)
    os.makedirs(spm_dir, exist_ok=True)
    spm.SentencePieceTrainer.Train( 
        f' --input={txt_path} --model_type=bpe'
        f' --model_prefix={spm_path} --vocab_size={vocab_size}'
        f' --input_sentence_size={input_sentence_size}'
        )


def build_x_vocab(txt_path, spm_model, x_vocab_path):    
    sp = spm.SentencePieceProcessor()
    sp.Load(spm_model)
     
    wp_counter = Counter()     
    title_lines = open(txt_path).readlines()
     
    max_wps_len = 0
    max_words_len = 0
    #for i, line in enumerate(title_lines):
    for line in tqdm.tqdm(title_lines, mininterval=1):
        line = line.strip()
        words = line.split()
        #if max_words_len < len(words):
        #    print(max_words_len, len(words), words)
        max_words_len = max(max_words_len, len(words))         
         
        wps = []
        for w in words:        
            wp = sp.EncodeAsPieces(w)
            max_wps_len = max(len(wp), max_wps_len)            
            wps += wp
         
        for wp in wps: 
            wp_counter[wp] += 1
             
    wp_vocab = [('PAD', max_wps_len)] + wp_counter.most_common()
    write_vocab(wp_vocab, x_vocab_path)

def write_vocab(vocab, vocab_fn):
    with open(vocab_fn, 'w') as fp:
        for v, c in vocab:
            fp.write(f'{v}\t{c}\n')


def split_data(data_path_list, div, chunk_size):
        total = 0
        for data_path in data_path_list:
            h = h5py.File(data_path, 'r')
            sz = h[div]['pid'].shape[0]
            total += sz
        chunks = [(i, min(i + chunk_size, total))
                  for i in range(0, total, chunk_size)]
        return chunks

def preprocessing_func(data):
    try:
        data_path_list, div, begin_offset, end_offset = data
        
        pregress = True if begin_offset == 0 else False
        reader = Reader(data_path_list, div, begin_offset, end_offset, pregress)
        ret = []
        for i, (pid, title, img_feat, b, m, s, d) in enumerate(reader.generate()):
            pid = pid.decode('utf-8')
            title = title.decode('utf-8')
            img_path = f'data/img/{div}/{pid}.pt'
            img_feat = torch.FloatTensor(img_feat)
            torch.save(img_feat, img_path)
            ret.append((pid, title, f'{b}>{m}>{s}>{d}'))
             
            #ret.append((pid, title, b, m, s, d))
    except Exception:
        raise Exception("".join(traceback.format_exception(*sys.exc_info())))
    return ret
    
def make_db(data_name):
    if data_name == 'train':
        div = 'train'
        data_path_list = opt.train_data_list
    elif data_name == 'dev':
        div = 'dev'
        data_path_list = opt.dev_data_list
    elif data_name == 'test':
        div = 'test'
        data_path_list = opt.test_data_list
    else:
        assert False, '%s is not valid data name' % data_name
        
    chunk_size = opt.chunk_size
    chunk_offsets = split_data(data_path_list, div, chunk_size)
    num_chunks = len(chunk_offsets)
    print('split data into %d chunks' % (num_chunks))
    pool = Pool(opt.num_workers)
    
    print('getting ... ')
    os.makedirs(f'data/img/{div}', exist_ok=True)
    try:
        rets = pool.map_async(preprocessing_func, [(
                                        data_path_list,
                                        div,
                                        begin,
                                        end)
                                       for begin, end in chunk_offsets]).get(9999999)
        pool.close()
        pool.join()
        print('getting ... done')
        rets = list(itertools.chain(*rets))
        os.makedirs('data', exist_ok=True)
        torch.save(rets, f'data/{data_name}.pt')
            
    except KeyboardInterrupt:
        pool.terminate()
        pool.join()
        raise

def build_y_vocab(cates, y_vocab_path):
    y_vocab = Counter(cates).most_common()
    write_vocab(y_vocab, y_vocab_path)

def build_vocab(data_name):
    if data_name != 'train':
        print('only [train] is supported')
        return
        
    print('loading db ...')
    db = torch.load(f'data/{data_name}.pt')
    
    # write titles
    pids, titles, cates = zip(*db)
    
    # pre-process titles    
    print('pre-procssing ...')    
    titles = [' '.join(cate_db.re_sc.sub(' ', title).strip().split()) for title in tqdm.tqdm(titles, mininterval=1)]
    print('writing preprocessed titles ...')    
    write_titles(titles, opt.title_path)
    
    print('training spm ...')
    train_spm(txt_path=opt.title_path)
    
    print('building x vocab ...')
    build_x_vocab(txt_path=opt.title_path, spm_model=opt.spm_model_path,
                  x_vocab_path=opt.x_vocab_path)

    print('building y vocab ...')
    build_y_vocab(cates, y_vocab_path=opt.y_vocab_path)


def main():
    fire.Fire({
        'make_db': make_db,
        'build_vocab': build_vocab,
    }) 

if __name__ == '__main__':
    main()