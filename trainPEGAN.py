import os
import json
import math
import time

import torch
import torch.optim as optim
from absl import flags, app
from torchvision import transforms
from torchvision.utils import make_grid, save_image
from tensorboardX import SummaryWriter
from tqdm import trange
from pytorch_gan_metrics import get_inception_score_and_fid

from datasets import get_dataset
from losses import HingeLoss, BCEWithLogits, Wasserstein, LeastSquareLoss, MinMax
from models import resnet, dcgan, biggan, SAdcgan
from models.gradnorm import normalize_gradient
from utils import ema, save_images, infiniteloop, set_seed, module_no_grad

net_G_models = {
    'dcgan.32': dcgan.Generator32,
    'dcgan.48': dcgan.Generator48,
    'resnet.32': resnet.ResGenerator32,
    'resnet.48': resnet.ResGenerator48,
    'biggan.32': biggan.Generator32,
    'sadcgan.32': SAdcgan.Generator32,
}

net_D_models = {
    'dcgan.32': dcgan.Discriminator32,
    'dcgan.48': dcgan.Discriminator48,
    'resnet.32': resnet.ResDiscriminator32,
    'resnet.48': resnet.ResDiscriminator48,
    'biggan.32': biggan.Discriminator32,
    'sadcgan.32': SAdcgan.Discriminator32,
}

loss_fns = {
    'hinge': HingeLoss,
    'bce': BCEWithLogits,
    'wass': Wasserstein,
    'ls': LeastSquareLoss,
}

datasets = ['cifar10.32', 'stl10.48']

FLAGS = flags.FLAGS
# resume
flags.DEFINE_bool('resume', False, 'resume from checkpoint')
flags.DEFINE_bool('eval', False, 'load model and evaluate it')
flags.DEFINE_string('save', "", 'load model and save sample images to dir')
# model and training
flags.DEFINE_enum('dataset', 'cifar10.32', datasets, "select dataset")  # 这里啥意思？
flags.DEFINE_enum('arch', 'resnet.32', net_G_models.keys(), "architecture")  # 模型的key
flags.DEFINE_enum('loss', 'bce', loss_fns.keys(), "loss function")
flags.DEFINE_integer('total_steps', 50000, "total number of training steps")
flags.DEFINE_integer('lr_decay_start', 0, 'apply linearly decay to lr')
flags.DEFINE_integer('batch_size_D', 64, "batch size for discriminator")
flags.DEFINE_integer('batch_size_G', 128, "batch size for generator")
flags.DEFINE_integer('num_workers', 8, "dataloader workers")
flags.DEFINE_float('lr_D', 4e-4, "Discriminator learning rate")
flags.DEFINE_float('lr_G', 2e-4, "Generator learning rate")
flags.DEFINE_multi_float('betas', [0.0, 0.9], "for Adam")
flags.DEFINE_integer('n_dis', 1, "update Generator every this steps")
flags.DEFINE_integer('z_dim', 128, "latent space dimension")
flags.DEFINE_float('cr', 0, "weight for consistency regularization")
flags.DEFINE_integer('seed', 0, "random seed")
# conditional
flags.DEFINE_integer('n_classes', 1, 'the number of classes in dataset')
# ema
flags.DEFINE_float('ema_decay', 0.9999, "ema decay rate")
flags.DEFINE_integer('ema_start', 0, "start step for ema")
# logging
flags.DEFINE_integer('sample_step', 500, "sample image every this steps")
flags.DEFINE_integer('sample_size', 64, "sampling size of images")
flags.DEFINE_integer('eval_step', 5000, "evaluate FID and Inception Score")
flags.DEFINE_integer('save_step', 20000, "save model every this step")
flags.DEFINE_integer('num_images', 50000, '# images for evaluation')
flags.DEFINE_integer('per', 10, '# period')
flags.DEFINE_string('fid_stats', './stats/cifar10.test.npz', 'FID cache')
flags.DEFINE_string('logdir', './logs/PEGAN_P10_CIFAR10_CNN', 'log folder')

device = torch.device('cuda:0')


def generate_images(net_G):
    images = []
    with torch.no_grad():
        for _ in trange(0, FLAGS.num_images, FLAGS.batch_size_G,
                        ncols=0, leave=False):
            z = torch.randn(FLAGS.batch_size_G, FLAGS.z_dim).to(device)
            y = torch.randint(
                FLAGS.n_classes, (FLAGS.batch_size_G,)).to(device)
            fake = (net_G(z, y) + 1) / 2
            images.append(fake.cpu())
    images = torch.cat(images, dim=0)
    return images[:FLAGS.num_images]


def eval_save():
    net_G = net_G_models[FLAGS.arch](FLAGS.z_dim, FLAGS.n_classes).to(device)
    ckpt = torch.load(os.path.join(FLAGS.logdir, 'best_model.pt'))  # eval模式下载入最优模型权重，

    net_G.load_state_dict(ckpt['net_G'])
    images = generate_images(net_G=net_G)
    if FLAGS.eval:
        (IS, IS_std), FID = get_inception_score_and_fid(
            images, FLAGS.fid_stats, verbose=True)  # 调用打分函数并获得分数，接着打印
        print("IS: %6.3f(%.3f), FID: %7.3f" % (IS, IS_std, FID))
    if FLAGS.save:
        save_images(images, FLAGS.save, verbose=True)


def evaluate(net_G):
    images = generate_images(net_G=net_G)
    (IS, IS_std), FID = get_inception_score_and_fid(
        images, FLAGS.fid_stats, verbose=True)
    del images
    return (IS, IS_std), FID


def consistency_loss(net_D, real, y_real, pred_real,
                     transform=transforms.Compose([
                         transforms.Lambda(lambda x: (x + 1) / 2),
                         transforms.ToPILImage(mode='RGB'),
                         transforms.RandomHorizontalFlip(),
                         transforms.RandomAffine(0, translate=(0.2, 0.2)),
                         transforms.ToTensor(),
                         transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
                     ])):
    aug_real = real.detach().clone().cpu()
    for idx, img in enumerate(aug_real):
        aug_real[idx] = transform(img)
    aug_real = aug_real.to(device)
    pred_aug = normalize_gradient(net_D, aug_real, y=y_real)
    loss = ((pred_aug - pred_real) ** 2).mean()
    return loss


def getfittness_G(net_G, net_D, z_):
    with torch.no_grad():
        # z_ = torch.randn(FLAGS.batch_size_G, FLAGS.z_dim).to(device)
        y_ = torch.randint(
            FLAGS.n_classes, (FLAGS.batch_size_G,)).to(device)
        fake = net_G(z_, y_).detach()
    fittness = normalize_gradient(net_D, fake, y=y_).mean()
    # fittness2 = net_D(fake, y_).mean()
    return fittness


def save_model_G(net_G, name):
    ckpt = {'net_G': net_G.state_dict()}
    torch.save(ckpt, os.path.join(FLAGS.logdir, name))
    del ckpt
    return None


def train():
    dataset = get_dataset(FLAGS.dataset)
    dataloader = torch.utils.data.DataLoader(
        dataset=dataset,
        batch_size=FLAGS.batch_size_D * FLAGS.n_dis,
        shuffle=True,
        num_workers=FLAGS.num_workers,
        drop_last=True)
    looper = infiniteloop(dataloader)

    # model
    net_G1 = net_G_models[FLAGS.arch](FLAGS.z_dim, FLAGS.n_classes).to(device)
    net_G2 = net_G_models[FLAGS.arch](FLAGS.z_dim, FLAGS.n_classes).to(device)
    net_G3 = net_G_models[FLAGS.arch](FLAGS.z_dim, FLAGS.n_classes).to(device)
    ema_G = net_G_models[FLAGS.arch](FLAGS.z_dim, FLAGS.n_classes).to(device)
    net_D = net_D_models[FLAGS.arch](FLAGS.n_classes).to(device)
    max_id = 0
    # loss for variation
    loss_fn = loss_fns[FLAGS.loss]()
    loss_fun1 = BCEWithLogits()
    loss_fun2 = MinMax()
    loss_fun3 = Wasserstein()

    ema(net_G1, ema_G, decay=0)

    # optimizer
    optim_G1 = optim.Adam(net_G1.parameters(), lr=FLAGS.lr_G, betas=FLAGS.betas)
    optim_G2 = optim.Adam(net_G2.parameters(), lr=FLAGS.lr_G, betas=FLAGS.betas)
    optim_G3 = optim.Adam(net_G3.parameters(), lr=FLAGS.lr_G, betas=FLAGS.betas)
    optim_D = optim.Adam(net_D.parameters(), lr=FLAGS.lr_D, betas=FLAGS.betas)
    choose_lossfunction = [0, 0, 0]

    # scheduler
    def decay_rate(step):
        period = max(FLAGS.total_steps - FLAGS.lr_decay_start, 1)
        return 1 - max(step - FLAGS.lr_decay_start, 0) / period

    sched_G1 = optim.lr_scheduler.LambdaLR(optim_G1, lr_lambda=decay_rate)
    sched_G2 = optim.lr_scheduler.LambdaLR(optim_G2, lr_lambda=decay_rate)
    sched_G3 = optim.lr_scheduler.LambdaLR(optim_G3, lr_lambda=decay_rate)

    sched_D = optim.lr_scheduler.LambdaLR(optim_D, lr_lambda=decay_rate)

    D_size = 0
    for param in net_D.parameters():
        D_size += param.data.nelement()  # 求神经网络D的参数量
    G_size = 0
    for param in net_G1.parameters():
        G_size += param.data.nelement()
    print('D params: %d, G params: %d' % (D_size, G_size))

    writer = SummaryWriter(FLAGS.logdir)
    if FLAGS.resume:
        ckpt = torch.load(os.path.join(FLAGS.logdir, 'model.pt'))
        net_G1.load_state_dict(ckpt['net_G1'])
        net_G2.load_state_dict(ckpt['net_G2'])
        net_G3.load_state_dict(ckpt['net_G3'])
        net_D.load_state_dict(ckpt['net_D'])
        optim_G1.load_state_dict(ckpt['optim_G1'])
        optim_G2.load_state_dict(ckpt['optim_G2'])
        optim_G3.load_state_dict(ckpt['optim_G3'])
        optim_D.load_state_dict(ckpt['optim_D'])
        sched_G1.load_state_dict(ckpt['sched_G1'])
        sched_G2.load_state_dict(ckpt['sched_G2'])
        sched_G3.load_state_dict(ckpt['sched_G3'])
        sched_D.load_state_dict(ckpt['sched_D'])
        fixed_z = ckpt['fixed_z']
        fixed_y = ckpt['fixed_y']
        # start value
        start = ckpt['step'] + 1
        best_IS, best_FID = ckpt['best_IS'], ckpt['best_FID']
        del ckpt
    else:
        # sample fixed z
        fixed_z = torch.randn(FLAGS.sample_size, FLAGS.z_dim).to(device)
        fixed_y = torch.randint(
            FLAGS.n_classes, (FLAGS.sample_size,)).to(device)
        # start value
        start, best_IS, best_FID = 1, 0, 999

        os.makedirs(os.path.join(FLAGS.logdir, 'sample'))
        with open(os.path.join(FLAGS.logdir, "flagfile.txt"), 'w') as f:
            f.write(FLAGS.flags_into_string())
        real = next(iter(dataloader))[0][:FLAGS.sample_size]
        writer.add_image('real_sample', make_grid((real + 1) / 2))
        writer.flush()

    with trange(start, FLAGS.total_steps + 1, ncols=0,
                initial=start - 1, total=FLAGS.total_steps) as pbar:
        for step in pbar:
            loss_sum = 0
            loss_real_sum = 0
            loss_fake_sum = 0
            loss_cr_sum = 0

            x, y = next(looper)
            x = iter(torch.split(x, FLAGS.batch_size_D))
            y = iter(torch.split(y, FLAGS.batch_size_D))
            # Discriminator
            for _ in range(FLAGS.n_dis):
                optim_D.zero_grad()
                x_real, y_real = next(x).to(device), next(y).to(device)

                with torch.no_grad():
                    z_1 = torch.randn(
                        FLAGS.batch_size_D//2, FLAGS.z_dim).to(device)
                    z_2 = torch.randn(
                        FLAGS.batch_size_D//2, FLAGS.z_dim).to(device)
                    z_3 = torch.randn(
                        FLAGS.batch_size_D//2, FLAGS.z_dim).to(device)
                    y_fake = torch.randint(
                        FLAGS.n_classes, (FLAGS.batch_size_D//2,)).to(device)
                    x_fake1 = net_G1(z_1, y_fake).detach()
                    x_fake2 = net_G2(z_2, y_fake).detach()
                    x_fake3 = net_G3(z_3, y_fake).detach()
                    x_fake = torch.cat([x_fake1, x_fake2, x_fake3], dim=0)
                x_real_fake = torch.cat([x_real, x_fake], dim=0)
                y_real_fake = torch.cat([y_real, y_fake, y_fake, y_fake], dim=0)
                pred = normalize_gradient(net_D, x_real_fake, y=y_real_fake)
                pred_real, pred_fake = torch.split(
                    pred, [x_real.shape[0], x_fake.shape[0]])

                loss, loss_real, loss_fake = loss_fn(pred_real, pred_fake)

                if FLAGS.cr > 0:
                    loss_cr = consistency_loss(
                        net_D, x_real, y_real, pred_real)
                else:
                    loss_cr = torch.tensor(0.)
                loss_all = loss + FLAGS.cr * loss_cr
                loss_all.backward()
                optim_D.step()

                loss_sum += loss.cpu().item()
                loss_real_sum += loss_real.cpu().item()
                loss_fake_sum += loss_fake.cpu().item()
                loss_cr_sum += loss_cr.cpu().item()

            loss = loss_sum / FLAGS.n_dis
            loss_real = loss_real_sum / FLAGS.n_dis
            loss_fake = loss_fake_sum / FLAGS.n_dis
            loss_cr = loss_cr_sum / FLAGS.n_dis

            writer.add_scalar('loss', loss, step)
            writer.add_scalar('loss_real', loss_real, step)
            writer.add_scalar('loss_fake', loss_fake, step)
            writer.add_scalar('loss_cr', loss_cr, step)
            writer.add_scalars('choose_loss_fun', {'fun1': choose_lossfunction[0],
                                                   'fun2': choose_lossfunction[1],
                                                   'fun3': choose_lossfunction[2]}, step)

            pbar.set_postfix(
                loss_real='%.3f' % loss_real,
                loss_fake='%.3f' % loss_fake,
                lossfun_times='{}'.format(choose_lossfunction))

            # Generator
            with module_no_grad(net_D):
                optim_G1.zero_grad()
                optim_G2.zero_grad()
                optim_G3.zero_grad()
                z_ = torch.randn(FLAGS.batch_size_G, FLAGS.z_dim).to(device)
                y_ = torch.randint(
                    FLAGS.n_classes, (FLAGS.batch_size_G,)).to(device)
                fake1 = net_G1(z_, y_)
                fake2 = net_G2(z_, y_)
                fake3 = net_G3(z_, y_)
                pred_fake1 = normalize_gradient(net_D, fake1, y=y_)
                pred_fake2 = normalize_gradient(net_D, fake2, y=y_)
                pred_fake3 = normalize_gradient(net_D, fake3, y=y_)

                loss1 = loss_fun1(pred_fake1)
                loss1.backward()
                optim_G1.step()

                loss2 = loss_fun2(pred_fake2)
                loss2.backward()
                optim_G2.step()

                loss3 = loss_fun3(pred_fake3)
                loss3.backward()
                optim_G3.step()
                # evaluation
                if step % FLAGS.per == 0 and step != 0:
                    fittness = []
                    fittness.append(getfittness_G(net_G1, net_D, z_))
                    fittness.append(getfittness_G(net_G2, net_D, z_))
                    fittness.append(getfittness_G(net_G3, net_D, z_))
                    maxf = max(fittness)
                    max_id = fittness.index(maxf)
                    choose_lossfunction[max_id] += 1

                    if max_id == 0:
                        net_G2.load_state_dict(net_G1.state_dict())
                        net_G3.load_state_dict(net_G1.state_dict())

                    elif max_id == 1:
                        net_G1.load_state_dict(net_G2.state_dict())
                        net_G3.load_state_dict(net_G2.state_dict())

                    else:
                        net_G1.load_state_dict(net_G3.state_dict())
                        net_G2.load_state_dict(net_G3.state_dict())

            # ema
            if step < FLAGS.ema_start:
                decay = 0
            else:
                decay = FLAGS.ema_decay
            ema(net_G1, ema_G, decay)
            # scheduler
            sched_G1.step()
            sched_G2.step()
            sched_G3.step()
            sched_D.step()

            # sample from fixed z
            if step == 100 or step % FLAGS.sample_step == 0:
                with torch.no_grad():
                    fake_net = net_G1(fixed_z, fixed_y).cpu()
                grid_net = (make_grid(fake_net) + 1) / 2
                writer.add_image('sample', grid_net, step)
                save_image(
                    grid_net,
                    os.path.join(FLAGS.logdir, 'sample', '%d.png' % step))

            # evaluate IS, FID and save model
            if step % FLAGS.eval_step == 0:
                (IS, IS_std), FID = evaluate(net_G1)

                (IS_ema, IS_std_ema), FID_ema = evaluate(ema_G)
                if not math.isnan(FID) and not math.isnan(best_FID):
                    save_as_best = (FID < best_FID)
                else:
                    save_as_best = (IS > best_IS)
                if save_as_best:
                    best_IS = IS
                    best_FID = FID
                ckpt = {
                    'net_G': net_G1.state_dict(),
                    'net_D': net_D.state_dict(),
                    'ema_G': ema_G.state_dict(),
                    'optim_G1': optim_G1.state_dict(),
                    'optim_G2': optim_G2.state_dict(),
                    'optim_G3': optim_G3.state_dict(),
                    'optim_D': optim_D.state_dict(),
                    'sched_G1': sched_G1.state_dict(),
                    'sched_G2': sched_G2.state_dict(),
                    'sched_G3': sched_G3.state_dict(),
                    'sched_D': sched_D.state_dict(),
                    'fixed_y': fixed_y,
                    'fixed_z': fixed_z,
                    'best_IS': best_IS,
                    'best_FID': best_FID,
                    'step': step,
                }
                if step == 1 or step % FLAGS.save_step == 0:
                    torch.save(
                        ckpt, os.path.join(FLAGS.logdir, '%06d.pt' % step))
                if save_as_best:
                    torch.save(
                        ckpt, os.path.join(FLAGS.logdir, 'best_model.pt'))
                torch.save(ckpt, os.path.join(FLAGS.logdir, 'model.pt'))
                metrics = {
                    'IS': IS,
                    'IS_std': IS_std,
                    'FID': FID,
                    'IS_EMA': IS_ema,
                    'IS_std_EMA': IS_std_ema,
                    'FID_EMA': FID_ema,
                }
                for name, value in metrics.items():
                    writer.add_scalar(name, value, step)
                writer.flush()
                with open(os.path.join(FLAGS.logdir, 'eval.txt'), 'a') as f:
                    metrics['step'] = step
                    f.write(json.dumps(metrics) + "\n")
                k = len(str(FLAGS.total_steps))
                pbar.write(
                    f"{step:{k}d}/{FLAGS.total_steps} "
                    f"IS: {IS:6.3f}({IS_std:.3f}), "
                    f"FID: {FID:.3f}, "
                    f"losefun_times:{choose_lossfunction}, "
                    f"best_FID:{best_FID:6.3f}, "
                    f"best_IS:{best_IS:6.3f}, "
                    f"IS_EMA: {IS_ema:6.3f}({IS_std_ema:.3f}), "
                    f"FID_EMA: {FID_ema:.3f}"
                                            )
    writer.close()


def main(argv):
    set_seed(FLAGS.seed)
    if FLAGS.eval or FLAGS.save:
        eval_save()
    else:
        train()


if __name__ == '__main__':
    app.run(main)