import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torchvision import datasets, transforms
from torch.utils.data import DataLoader

# 1. 定义超参数
BATCH_SIZE = 64
EPOCHS = 5
LEARNING_RATE = 0.001
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# 2. 数据预处理与加载
transform = transforms.Compose([
    transforms.ToTensor(),
    transforms.Normalize((0.1307,), (0.3081,)) # MNIST 数据集的均值和标准差
])

# 下载并加载训练集和测试集
train_dataset = datasets.MNIST(root='./data', train=True, download=True, transform=transform)
test_dataset = datasets.MNIST(root='./data', train=False, download=True, transform=transform)

train_loader = DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=True)
test_loader = DataLoader(test_dataset, batch_size=BATCH_SIZE, shuffle=False)

# 3. 定义卷积神经网络模型 (CNN)
class SimpleCNN(nn.Module):
    def __init__(self):
        super(SimpleCNN, self).__init__()
        # 卷积层 1: 输入 1 通道 (灰度图)，输出 32 通道，卷积核 3x3
        self.conv1 = nn.Conv2d(in_channels=1, out_channels=32, kernel_size=3, stride=1, padding=1)
        # 卷积层 2: 输入 32 通道，输出 64 通道，卷积核 3x3
        self.conv2 = nn.Conv2d(in_channels=32, out_channels=64, kernel_size=3, stride=1, padding=1)
        # 最大池化层: 窗口大小 2x2
        self.pool = nn.MaxPool2d(kernel_size=2, stride=2)
        
        # 全连接层 1: MNIST 图片原始大小 28x28
        # 经过两次池化 (28 -> 14 -> 7)，特征图大小变为 7x7，通道数为 64
        self.fc1 = nn.Linear(64 * 7 * 7, 128)
        # 全连接层 2: 输出 10 个类别 (数字 0-9)
        self.fc2 = nn.Linear(128, 10)

    def forward(self, x):
        # 形状变化: [batch, 1, 28, 28] -> [batch, 32, 14, 14]
        x = self.pool(F.relu(self.conv1(x)))
        # 形状变化: [batch, 32, 14, 14] -> [batch, 64, 7, 7]
        x = self.pool(F.relu(self.conv2(x)))
        
        # 展平多维的特征图，送入全连接层
        x = x.view(-1, 64 * 7 * 7)
        
        x = F.relu(self.fc1(x))
        x = self.fc2(x)
        return x

# 4. 实例化模型、损失函数和优化器
model = SimpleCNN().to(DEVICE)
criterion = nn.CrossEntropyLoss()
optimizer = optim.Adam(model.parameters(), lr=LEARNING_RATE)

# 5. 定义训练过程
def train(model, device, train_loader, optimizer, criterion, epoch):
    model.train() # 设置为训练模式
    for batch_idx, (data, target) in enumerate(train_loader):
        data, target = data.to(device), target.to(device)
        
        optimizer.zero_grad()        # 梯度清零
        output = model(data)         # 前向传播
        loss = criterion(output, target) # 计算损失
        loss.backward()              # 反向传播计算梯度
        optimizer.step()             # 更新参数
        
        if batch_idx % 100 == 0:
            print(f'Train Epoch: {epoch} [{batch_idx * len(data)}/{len(train_loader.dataset)} '
                  f'({100. * batch_idx / len(train_loader):.0f}%)]\tLoss: {loss.item():.6f}')

# 6. 定义测试过程
def test(model, device, test_loader, criterion):
    model.eval() # 设置为评估模式
    test_loss = 0
    correct = 0
    with torch.no_grad(): # 测试时不需要计算梯度
        for data, target in test_loader:
            data, target = data.to(device), target.to(device)
            output = model(data)
            test_loss += criterion(output, target).item() # 累加 batch 的损失
            pred = output.argmax(dim=1, keepdim=True)     # 获取最大概率的类别索引
            correct += pred.eq(target.view_as(pred)).sum().item()

    test_loss /= len(test_loader) # 计算平均损失
    accuracy = 100. * correct / len(test_loader.dataset)
    print(f'\nTest set: Average loss: {test_loss:.4f}, Accuracy: {correct}/{len(test_loader.dataset)} '
          f'({accuracy:.2f}%)\n')

# 7. 开始训练
if __name__ == '__main__':
    print(f"使用设备: {DEVICE}")
    for epoch in range(1, EPOCHS + 1):
        train(model, DEVICE, train_loader, optimizer, criterion, epoch)
        test(model, DEVICE, test_loader, criterion)
